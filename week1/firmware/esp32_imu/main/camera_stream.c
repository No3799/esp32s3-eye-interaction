/* ESP32-S3-EYE 板载 OV2640 摄像头 —— MJPEG 视频流服务
 *
 * 为什么能在一块 MCU 上跑视频监控：OV2640 传感器自带 JPEG 硬编码，
 * 它吐出来的就是压缩好的 JPEG 码流，ESP32 只负责搬运转发，不需要自己编码。
 *
 * 三个要点：
 *  1) 必须先开 PSRAM（sdkconfig: CONFIG_SPIRAM=y），帧缓冲放外部 RAM。
 *     内部 DRAM 只有 200 多 KB，一张 320x240 的 JPEG 就可能上百 KB，
 *     不开 PSRAM 必崩 —— 这是摄像头最典型的踩坑点。
 *  2) 摄像头 SCCB 与 IMU 共用 I2C_NUM_0 那条物理总线（GPIO4/5）。
 *     这里 pin_sccb_sda/scl 必须填 -1 让摄像头直接复用已初始化的总线，
 *     如果填具体引脚号，SCCB 会自己装一套驱动去抢引脚，IMU 就读不到数据了。
 *  3) 视频流经 81 端口，跟 IMU 上报走的 8000 端口是两条完全独立的路，
 *     网页直接 <img src="http://板子IP:81/stream"> 就能看。
 *
 * ★ 最重要的一条设计约束（血泪坑）★
 *  ESP-IDF 自带的 esp_http_server 是【单线程】的：它的 httpd_thread 里只有一句
 *      while (1) { httpd_server(hd); }
 *  URI handler 是在这个唯一的线程里同步调用的。也就是说——只要有一个 handler
 *  不返回，整个服务器就彻底卡死：新的连接没人 accept，/capture 也没人应答。
 *
 *  而 MJPEG 推流的 handler 天然就是个死循环（客户端不断开就一直推帧）。
 *  直接写成同步 handler 的后果非常隐蔽：
 *      · 第一个人打开网页，画面正常；
 *      · 他刷新一次页面 / 再开一个标签页，旧的那条连接还堵在那里；
 *      · 从此 81 端口对所有人都无响应，网页显示「未连接」；
 *      · 只有重新上电才能恢复 —— 这就是之前「有时能用、大多时候不行」的真正原因。
 *
 *  解决办法：用官方提供的异步 handler 接口 httpd_req_async_handler_begin()。
 *  handler 拿到请求的副本后立刻返回，把推流交给独立任务去做，
 *  httpd 主线程马上回到 select 循环，随时能应答后续请求。
 */

#include <string.h>
#include <stdio.h>
#include <errno.h>
#include "esp_log.h"
#include "esp_system.h"
#include "esp_timer.h"
#include "esp_heap_caps.h"
#include "driver/i2c.h"
#include "esp_camera.h"
#include "esp_http_server.h"
#include "freertos/FreeRTOS.h"
#include "freertos/task.h"
#include "freertos/semphr.h"
#include "lwip/sockets.h"
#include "camera_stream.h"

static const char *TAG = "week1:cam";

/* ESP32-S3-EYE 摄像头引脚（来源：esp-bsp bsp/esp32_s3_eye 官方定义） */
#define CAM_PIN_PWDN   -1      /* 未接，省电控制用不上 */
#define CAM_PIN_RESET  -1      /* 未接，走软件复位 */
#define CAM_PIN_XCLK   15
#define CAM_PIN_SIOD   4       /* 与 IMU 共用 SDA */
#define CAM_PIN_SIOC   5       /* 与 IMU 共用 SCL */
#define CAM_PIN_D7     16
#define CAM_PIN_D6     17
#define CAM_PIN_D5     18
#define CAM_PIN_D4     12
#define CAM_PIN_D3     10
#define CAM_PIN_D2     8
#define CAM_PIN_D1     9
#define CAM_PIN_D0     11
#define CAM_PIN_VSYNC  6
#define CAM_PIN_HREF   7
#define CAM_PIN_PCLK   13

#define CAM_STREAM_PORT  81

/* 能忍受多少次连续的「暂时发不出去」（每次约 send_wait_timeout=2 秒）。
 * 给足 8 次 ≈ 16 秒的宽限：WiFi 抖动过去后画面能自己恢复，
 * 而对方真断开时又不至于拖太久才释放名额。 */
#define CAM_EAGAIN_TOLERANCE 8

/* 同时最多允许几个视频流客户端。
 * 每个客户端都要占一路 socket + 一个推流任务 + 一份 WiFi 带宽。
 * 板子上行带宽本来就不宽裕，不加限制会谁都卡。超出的连接会被快速拒绝（503），
 * 而不是挂在那里把服务器拖死。 */
#define CAM_MAX_STREAM_CLIENTS 2

/* MJPEG 流格式：multipart/x-mixed-replace，每帧用 boundary 分隔 */
#define PART_BOUNDARY "123456789000000000000987654321"
static const char *STREAM_CONTENT_TYPE = "multipart/x-mixed-replace;boundary=" PART_BOUNDARY;
static const char *STREAM_BOUNDARY     = "\r\n--" PART_BOUNDARY "\r\n";
static const char *STREAM_PART_HDR     = "Content-Type: image/jpeg\r\nContent-Length: %u\r\n\r\n";

static httpd_handle_t s_stream_httpd = NULL;

/* 客户端名额计数器。多个任务/多个 handler 都可能改它，必须加锁。 */
static SemaphoreHandle_t s_cli_mutex = NULL;
static int                s_cli_count = 0;

static camera_config_t s_cam_cfg = {
    .pin_pwdn       = CAM_PIN_PWDN,
    .pin_reset      = CAM_PIN_RESET,
    .pin_xclk       = CAM_PIN_XCLK,
    /* SCCB（摄像头配置总线）与 IMU 共用 GPIO4/5 那条物理 I2C。
     * 如果这里填具体引脚号，SCCB 会自己装一套 I2C 驱动去抢这些引脚，
     * 结果就是 IMU 读不到数据（实测：两边都装老驱动，引脚被后装的抢走）。
     * 正确做法：填 -1，让摄像头直接复用已经初始化好的 I2C_NUM_0 总线，
     * 两个设备挂在同一条总线上（IMU=0x12，OV2640=0x30），互不干扰。 */
    .pin_sccb_sda   = -1,
    .pin_sccb_scl   = -1,
    .sccb_i2c_port  = I2C_NUM_0,
    .pin_d7         = CAM_PIN_D7,
    .pin_d6         = CAM_PIN_D6,
    .pin_d5         = CAM_PIN_D5,
    .pin_d4         = CAM_PIN_D4,
    .pin_d3         = CAM_PIN_D3,
    .pin_d2         = CAM_PIN_D2,
    .pin_d1         = CAM_PIN_D1,
    .pin_d0         = CAM_PIN_D0,
    .pin_vsync      = CAM_PIN_VSYNC,
    .pin_href       = CAM_PIN_HREF,
    .pin_pclk       = CAM_PIN_PCLK,

    .xclk_freq_hz   = 20000000,
    .ledc_timer     = LEDC_TIMER_0,
    .ledc_channel   = LEDC_CHANNEL_0,

    .pixel_format   = PIXFORMAT_JPEG,   /* 关键：让传感器输出 JPEG，不要 GRB565 */
    .frame_size     = FRAMESIZE_VGA,    /* 640x480：QVGA 在电脑上放大后明显糊 */
    .jpeg_quality   = 10,               /* 0~63，越小画质越高、码流越大（原 12，降一档补细节） */
    .fb_count       = 2,                /* 双缓冲，采集与发送并行 */
    .fb_location    = CAMERA_FB_IN_PSRAM,
    .grab_mode      = CAMERA_GRAB_LATEST,
};

/* ---------- 客户端名额管理 ---------- */
static bool client_slot_take(void)
{
    bool ok = false;
    xSemaphoreTake(s_cli_mutex, portMAX_DELAY);
    if (s_cli_count < CAM_MAX_STREAM_CLIENTS) {
        s_cli_count++;
        ok = true;
    }
    xSemaphoreGive(s_cli_mutex);
    return ok;
}

static void client_slot_give(void)
{
    xSemaphoreTake(s_cli_mutex, portMAX_DELAY);
    if (s_cli_count > 0) {
        s_cli_count--;
    }
    xSemaphoreGive(s_cli_mutex);
}

static int client_slot_count(void)
{
    xSemaphoreTake(s_cli_mutex, portMAX_DELAY);
    int n = s_cli_count;
    xSemaphoreGive(s_cli_mutex);
    return n;
}

/* ---------- 推流任务（独立于 httpd 主线程） ----------
 * 注意三件事：
 *  1) req 是异步副本，用完必须 httpd_req_async_handler_complete()，否则会话永远不释放；
 *  2) 一旦发送失败，说明对方关页面 / 断网了，必须立刻收摊，把名额让出来；
 *  3) 开了 SO_KEEPALIVE 之后，就算对方是「拔网线、笔记本合盖」这种没有 FIN 包的
 *     假断开，TCP 探测也会在几十秒内失败，任务随之退出 —— 这一步是防泄漏的关键。 */
static void stream_task(void *arg)
{
    httpd_req_t *req = (httpd_req_t *)arg;
    int sockfd = httpd_req_to_sockfd(req);

    int one = 1;
    setsockopt(sockfd, SOL_SOCKET, SO_KEEPALIVE, &one, sizeof(one));
    /* TCP_NODELAY：关掉 Nagle 合并。MJPEG 每帧分 3 次小写入（boundary/头/数据），
     * 开着 Nagle 会被攒包，画面延迟明显变卡。 */
    setsockopt(sockfd, IPPROTO_TCP, TCP_NODELAY, &one, sizeof(one));
#if defined(TCP_KEEPIDLE) && defined(TCP_KEEPINTVL) && defined(TCP_KEEPCNT)
    int idle = 15, intvl = 5, cnt = 3;
    setsockopt(sockfd, IPPROTO_TCP, TCP_KEEPIDLE,  &idle,  sizeof(idle));
    setsockopt(sockfd, IPPROTO_TCP, TCP_KEEPINTVL, &intvl, sizeof(intvl));
    setsockopt(sockfd, IPPROTO_TCP, TCP_KEEPCNT,   &cnt,   sizeof(cnt));
#endif

    httpd_resp_set_type(req, STREAM_CONTENT_TYPE);
    httpd_resp_set_hdr(req, "Access-Control-Allow-Origin", "*");
    httpd_resp_set_hdr(req, "Cache-Control", "no-store");

    ESP_LOGI(TAG, "推流开始 fd=%d，当前客户端 %d/%d",
             sockfd, client_slot_count(), CAM_MAX_STREAM_CLIENTS);

    uint32_t frames = 0;
    int      eagain_hits = 0;      /* 连续「暂时发不出去」的次数 */
    int64_t  t0 = esp_timer_get_time();

    while (1) {
        camera_fb_t *fb = esp_camera_fb_get();
        if (!fb) {
            ESP_LOGE(TAG, "取帧失败，结束本次推流");
            break;
        }

        char part_hdr[64];
        int n = snprintf(part_hdr, sizeof(part_hdr), STREAM_PART_HDR, (unsigned)fb->len);

        /* 分三次写：boundary、帧头、帧数据。逐个检查，出错立刻停手 */
        esp_err_t bad = ESP_OK;
        int bad_errno = 0;
        if (n > 0 && n < (int)sizeof(part_hdr)) {
            errno = 0;
            bad = httpd_resp_send_chunk(req, STREAM_BOUNDARY, strlen(STREAM_BOUNDARY));
            bad_errno = errno;
        }
        if (bad == ESP_OK && n > 0 && n < (int)sizeof(part_hdr)) {
            errno = 0;
            bad = httpd_resp_send_chunk(req, part_hdr, n);
            bad_errno = errno;
        }
        if (bad == ESP_OK && n > 0 && n < (int)sizeof(part_hdr)) {
            errno = 0;
            bad = httpd_resp_send_chunk(req, (const char *)fb->buf, fb->len);
            bad_errno = errno;
        }
        esp_camera_fb_return(fb);

        if (bad != ESP_OK) {
            /* 区分两种失败，这是稳定性的关键：
             *  EAGAIN/EWOULDBLOCK = WiFi 一时堵了或对方接收窗口满了，连接还活着。
             *      老版本一遇到就退出推流，结果客户端马上重连、又堵、又退出，
             *      陷入「不断重连却一直没画面」的死循环。
             *      正确做法是丢掉这一帧继续推，只有连续多次都发不出去才认输。
             *  其他错误（104 ECONNRESET、128 ENOTCONN 等）= 对方真的断了，立刻收摊。 */
            if (bad_errno == EAGAIN || bad_errno == EWOULDBLOCK) {
                if (++eagain_hits <= CAM_EAGAIN_TOLERANCE) {
                    continue;
                }
                ESP_LOGW(TAG, "连续 %d 次发送超时（errno=%d），判定链路不可用",
                         eagain_hits - 1, bad_errno);
            } else {
                ESP_LOGI(TAG, "发送失败 errno=%d，结束本次推流", bad_errno);
            }
            break;
        }
        eagain_hits = 0;
        frames++;
    }

    double secs = (double)(esp_timer_get_time() - t0) / 1000000.0;
    ESP_LOGI(TAG, "推流结束 fd=%d：%lu 帧 / %.1f 秒 (%.1f fps)",
             sockfd, (unsigned long)frames, secs, secs > 0 ? frames / secs : 0.0);

    /* 收摊：先归还名额，再释放异步会话，最后删自己 */
    client_slot_give();
    httpd_req_async_handler_complete(req);
    ESP_LOGI(TAG, "视频流连接结束，剩余客户端 %d", client_slot_count());
    vTaskDelete(NULL);
}

/* MJPEG 流 handler：拿到请求副本立刻返回，绝不停在这里 */
static esp_err_t stream_handler(httpd_req_t *req)
{
    if (!client_slot_take()) {
        ESP_LOGW(TAG, "已有 %d 个客户端在观看，拒绝本次连接（返回 503 快速失败）",
                 CAM_MAX_STREAM_CLIENTS);
        httpd_resp_set_status(req, "503 Service Unavailable");
        httpd_resp_set_type(req, "text/plain; charset=utf-8");
        httpd_resp_set_hdr(req, "Access-Control-Allow-Origin", "*");
        httpd_resp_send(req, "stream busy: too many clients", HTTPD_RESP_USE_STRLEN);
        return ESP_OK;
    }

    httpd_req_t *async_req = NULL;
    esp_err_t err = httpd_req_async_handler_begin(req, &async_req);
    if (err != ESP_OK) {
        ESP_LOGW(TAG, "异步槽位不足: %s", esp_err_to_name(err));
        client_slot_give();
        /* 注意：async_begin 成功之后就不能再动原始 req 了，这里还没成功，可以正常报错 */
        httpd_resp_send_err(req, HTTPD_500_INTERNAL_SERVER_ERROR, "async slot busy");
        return ESP_FAIL;
    }

    /* ★ 关键：推流丢给独立任务，本 handler 立刻返回，
     *   httpd 主线程得以回到 select 循环，后续请求才能被处理。 */
    BaseType_t ret = xTaskCreatePinnedToCore(stream_task, "cam_stream", 8192, async_req, 6, NULL, 1);
    if (ret != pdPASS) {
        ESP_LOGE(TAG, "创建推流任务失败，释放名额");
        client_slot_give();
        httpd_req_async_handler_complete(async_req);
        return ESP_FAIL;
    }
    return ESP_OK;
}

/* 单帧快照：适合做定时抓拍、存档 */
static esp_err_t capture_handler(httpd_req_t *req)
{
    camera_fb_t *fb = esp_camera_fb_get();
    if (!fb) {
        httpd_resp_send_err(req, HTTPD_500_INTERNAL_SERVER_ERROR, "取帧失败");
        return ESP_FAIL;
    }
    httpd_resp_set_type(req, "image/jpeg");
    httpd_resp_set_hdr(req, "Content-Disposition", "inline; filename=capture.jpg");
    httpd_resp_set_hdr(req, "Access-Control-Allow-Origin", "*");
    esp_err_t res = httpd_resp_send(req, (const char *)fb->buf, fb->len);
    esp_camera_fb_return(fb);
    return res;
}

/* 心跳 + 自检：网页用它判断「服务器还活着吗」。
 * 这是一个毫秒级就能返回的请求，如果连它都超时，说明服务器被堵住了，
 * 页面就自动重建视频流连接。 */
static esp_err_t ping_handler(httpd_req_t *req)
{
    char buf[128];
    int n = snprintf(buf, sizeof(buf),
                     "pong\nuptime_ms=%lld\nclients=%d/%d\nfree_heap=%u\n",
                     (long long)(esp_timer_get_time() / 1000),
                     client_slot_count(), CAM_MAX_STREAM_CLIENTS,
                     (unsigned)esp_get_free_heap_size());
    httpd_resp_set_type(req, "text/plain; charset=utf-8");
    httpd_resp_set_hdr(req, "Access-Control-Allow-Origin", "*");
    httpd_resp_set_hdr(req, "Cache-Control", "no-store");
    return httpd_resp_send(req, buf, n);
}

/* 板子自带的最小预览页：直接用浏览器打开 http://板子IP:81/ 就能看画面，
 * 不需要先开那个网页 —— 排障时用来快速区分「网页的问题」还是「板子的问题」 */
static const char *INDEX_HTML =
    "<!DOCTYPE html><html lang=\"zh-CN\"><head><meta charset=\"utf-8\">"
    "<meta name=\"viewport\" content=\"width=device-width,initial-scale=1\">"
    "<title>ESP32-S3-EYE 摄像头</title></head>"
    "<body style=\"font-family:sans-serif;background:#111;color:#eee;margin:0;padding:16px\">"
    "<h3 style=\"margin:0 0 10px\">OV2640 实时画面（板端直出）</h3>"
    "<img src=\"/stream\" style=\"width:100%;max-width:640px;background:#000\">"
    "<p style=\"color:#888\">这是板子自己的页面。这里能看到画面，说明摄像头没问题；"
    "看这里就没画面，则是板端故障。</p></body></html>";

static esp_err_t index_handler(httpd_req_t *req)
{
    httpd_resp_set_type(req, "text/html; charset=utf-8");
    httpd_resp_set_hdr(req, "Access-Control-Allow-Origin", "*");
    return httpd_resp_send(req, INDEX_HTML, HTTPD_RESP_USE_STRLEN);
}

static esp_err_t start_stream_server(void)
{
    httpd_config_t cfg = HTTPD_DEFAULT_CONFIG();
    cfg.server_port    = CAM_STREAM_PORT;
    cfg.stack_size     = 8192;          /* 默认 4096 跑拼接格式字符串 + 发送很紧 */
    cfg.max_uri_handlers = 8;
    /* 下面三项是防「连接泄漏」的标准做法：
     *  lru_purge_enable: 会话不够时，把最久没动静的连接挤走，而不是直接卡住
     *  recv/send 超时:   让半天不回话的连接尽快报错退出，释放名额 */
    cfg.lru_purge_enable  = true;
    cfg.recv_wait_timeout = 2;
    cfg.send_wait_timeout = 2;

    if (httpd_start(&s_stream_httpd, &cfg) != ESP_OK) {
        ESP_LOGE(TAG, "视频流服务启动失败");
        return ESP_FAIL;
    }

    httpd_uri_t index_uri = {
        .uri      = "/",
        .method   = HTTP_GET,
        .handler  = index_handler,
        .user_ctx = NULL,
    };
    httpd_register_uri_handler(s_stream_httpd, &index_uri);

    httpd_uri_t stream_uri = {
        .uri      = "/stream",
        .method   = HTTP_GET,
        .handler  = stream_handler,
        .user_ctx = NULL,
    };
    httpd_register_uri_handler(s_stream_httpd, &stream_uri);

    httpd_uri_t capture_uri = {
        .uri      = "/capture",
        .method   = HTTP_GET,
        .handler  = capture_handler,
        .user_ctx = NULL,
    };
    httpd_register_uri_handler(s_stream_httpd, &capture_uri);

    httpd_uri_t ping_uri = {
        .uri      = "/ping",
        .method   = HTTP_GET,
        .handler  = ping_handler,
        .user_ctx = NULL,
    };
    httpd_register_uri_handler(s_stream_httpd, &ping_uri);

    ESP_LOGI(TAG, "视频流已就绪: http://<板子IP>:%d/stream  (单帧: /capture, 自检: /ping)",
             CAM_STREAM_PORT);
    return ESP_OK;
}

esp_err_t camera_stream_start(void)
{
    /* 打印可用内存，排错时第一眼就看这个 */
    ESP_LOGI(TAG, "PSRAM 总容量=%u 字节, 空闲堆=%u",
             (unsigned)heap_caps_get_total_size(MALLOC_CAP_SPIRAM),
             (unsigned)esp_get_free_heap_size());

    s_cli_mutex = xSemaphoreCreateMutex();
    if (s_cli_mutex == NULL) {
        ESP_LOGE(TAG, "创建互斥量失败");
        return ESP_ERR_NO_MEM;
    }

    esp_err_t err = esp_camera_init(&s_cam_cfg);
    if (err != ESP_OK) {
        ESP_LOGE(TAG, "摄像头初始化失败: %s", esp_err_to_name(err));
        ESP_LOGE(TAG, "最常见原因：PSRAM 没开，或引脚定义与板子不符");
        return err;
    }

    sensor_t *s = esp_camera_sensor_get();
    if (s) {
        /* OV2640 模组在本板上是**倒装**的：实测画面上下颠倒、左右正常，
         * 所以只开 vflip。不要顺手把 hmirror 也打开 —— 那会变成左右镜像。 */
        s->set_vflip(s, 1);
        ESP_LOGI(TAG, "摄像头就绪: pid=0x%02x ver=0x%02x",
                 (unsigned)s->id.PID, (unsigned)s->id.VER);
    }

    ESP_LOGI(TAG, "初始化后空闲堆=%u, PSRAM 空闲=%u",
             (unsigned)esp_get_free_heap_size(),
             (unsigned)heap_caps_get_free_size(MALLOC_CAP_SPIRAM));

    return start_stream_server();
}
