/* AI 交互课 第1周 —— ESP32-S3-EYE 板端固件（板载三轴加速度计）
 *
 * 功能：连 WiFi -> I2C 读板载加速度计（GPIO4=SDA, GPIO5=SCL, 地址 0x12）
 *       -> 每 1 秒 HTTP POST 一条 JSON 到服务端
 *
 * ====== 第2周新增：接收并执行「重新采集」指令 ======
 * 板子只做 POST、不监听端口，服务器推不了东西给它。所以反过来利用这条上行
 * 通道：服务器把待执行指令塞进 /api/data 的**响应体**里，板子每次上报时
 * 顺手收下来并执行。不需要给板子开新端口，也不用 MQTT/WebSocket。
 *
 * 一条指令的执行过程：
 *   收到指令 → 回报 received → 按 samples/interval_ms 采一批
 *            → 最后一条带 done → 服务器据此判定完成
 * 这批数据的 trigger="command"（常规定时上报是 "timer"），
 * 并带同一个 request_id，因此"这些数据是被这次请求触发的新采集"
 * 是可验证的，而不是从历史里翻出来的旧记录。
 *
 * ====== 第3周：板载实体按键 + 本地反馈 + 教学求助闭环 ======
 * 让**现场的人**也能和设备交互，而不是只有远端网页能指挥设备。
 *
 * ⚠️ 安全边界（写在代码里，不只写在文档里）：
 *    本工程只连接**本组教学接收端**（SERVER_BASE 指向本组服务器）。
 *    它没有任何外呼能力 —— 不打电话、不发短信、不推任何第三方服务。
 *    按键唯一的动作是往本组服务器写一条记录，并在网页上显示出来。
 *
 *   按键：4 个用户键不各占一个 GPIO，而是挂在同一个 ADC 上的电阻梯
 *         （原理图标注 `4-Keys: ADC1_CH0`，即 GPIO1）。靠电压区分键位：
 *         UP+ 0.38V / DN- 0.82V / PLAY 1.98V / MENU 2.41V，无按键时约 3.3V。
 *   反馈：本板**没有蜂鸣器**，也没有可编程 RGB 灯，唯一软件可控的发光器件
 *         是绿色状态灯（GPIO3），必须开漏驱动 —— 见下方 LED 段的警告。
 *
 * 按一下键会发生什么（按键的语义是**发起一条教学求助测试消息**，
 * 不是"采一批数据" —— 数据只是这条消息的上下文）：
 *
 *   ① 按下瞬间        → 绿灯短亮一下              本地确认（断网也有效）
 *   ② 消息送达服务器  → 绿灯连闪 2 次              VPS 接收
 *                      网页上出现这条求助
 *   ③ 有查看者回应    → 绿灯慢闪 4 次（等远端回传） 查看者回应
 *   ④ 求助被取消      → 绿灯长亮 1 秒（等远端回传）
 *
 *   ②③④ 都要**真的发生过**才算 —— 网页上没有②③④的证据时不会显示
 *   "对方已收到"，本地也不会给出对应的灯效。这就是三级反馈必须分开的原因。
 *
 * ====== 关于芯片型号（重要，调试踩坑的根源）======
 * 原理图和官方手册标注的是 QMA7981，但**本板实装为 QMA6100P**。
 * 两者是 pin-to-pin 兼容的换代关系（QMA7981 已 EOL 停产），封装、
 * I2C 地址(0x12)、14 位 ADC 都一样，所以硬件上换料看不出来。
 * 本板的实测证据（上电时 imu_identify() 会自动打印一遍）：
 *   1) CHIP_ID(0x00) = 0x90  -> QMA6100P（QMA7981 公开手册为 0xE7）
 *   2) QMA6100P 手册特有寄存器 0x33/0x45/0x46/0x4A/0x56/0x5F 均有有效应答
 *   3) 量程位编码 0x01/0x02/0x04/0x08/0x0F(±2/4/8/16/32g) 全部被接受，
 *      而 0x00 是非法值 —— 当初按 QMA7981 手册写 0x00 导致模长异常 1.5g
 * 结论：本驱动按 QMA6100P 的真实寄存器行为编写，不要照 QMA7981 手册改。
 *
 * 注意：QMA6100P 只有三轴加速度，没有陀螺仪 —— gx/gy/gz 固定发 0。
 *
 * 使用前改 3 个宏：WIFI_SSID / WIFI_PASS / SERVER_URL
 * 编译烧录（用桌面的 ESP-IDF 5.4 CMD）：
 *     idf.py set-target esp32s3
 *     idf.py build
 *     idf.py -p COM5 flash monitor
 *
 * SERVER_URL 填电脑的局域网 IP（如 http://192.168.1.23:8000/api/data），
 * 不能用 127.0.0.1 —— 那是板子自己。
 */

#include <stdio.h>
#include <string.h>
#include <math.h>
#include "freertos/FreeRTOS.h"
#include "freertos/task.h"
#include "freertos/event_groups.h"
#include "esp_log.h"
#include "esp_wifi.h"
#include "esp_event.h"
#include "esp_netif.h"
#include "esp_http_client.h"
#include "esp_timer.h"
#include "nvs_flash.h"
#include "driver/i2c.h"
#include "driver/temperature_sensor.h"   /* ESP32-S3 内置温度传感器*/
#include "esp_system.h"                  /* esp_get_free_heap_size() 等内存统计 */
#include "esp_random.h"                  /* esp_random()：按键事件 id 的上电随机前缀 */
#include "esp_heap_caps.h"               /* heap_caps_get_total_size() 堆总大小 */
#include "cJSON.h"                       /* IDF 自带 JSON 解析（组件 json） */
#include "freertos/queue.h"              /* 第3周：按键/LED 之间用队列通信 */
#include "driver/gpio.h"                 /* 第3周：绿色状态灯 GPIO3 */
#include "esp_adc/adc_oneshot.h"         /* 第3周：按键阵列是电阻梯，走 ADC 读 */
#include "esp_adc/adc_cali.h"
#include "esp_adc/adc_cali_scheme.h"
#include "camera_stream.h"
#include "mic_record.h"                  /* 第5周：板载麦克风录音 */

/* ============ 需要按实际情况修改 ============ */
#define WIFI_SSID      "431"
#define WIFI_PASS      "88888888"
/* 服务器地址与两个端点分开写：第3周起"暂停周期上报"期间要改用 /api/poll
 * 心跳来保持命令通道，不能再把完整 URL 写死成一个宏。 */
#define SERVER_BASE    "http://10.1.41.18:8000"
#define PATH_DATA      "/api/data"      /* 正常上报：写一条传感记录 + 搭车取指令 */
#define PATH_POLL      "/api/poll"      /* 暂停期间的心跳：不写数据，只为取指令 */
#define PATH_HELP      "/api/help"      /* 教学求助测试消息（不是采集数据） */
#define PATH_AUDIO     "/api/audio"     /* 第5周：板载麦克风录的一段 WAV */

/* ============ 静态地址兜底（网络异常时的自保） ============
 *
 * 起因（2026-09-23 实测踩到）：在一次串口工具反复重连之后，本网段的 DHCP
 * 就不再给这块板子发地址了 —— 板子能关联 AP（RSSI −43）却永远拿不到 IP，
 * 连续 4 次重启都一样。旧固件在等 IP 那一步是 portMAX_DELAY：**永久卡死**，
 * 既不重试也不说明，整块板子看起来"死了"。
 *
 * 改法有两层：
 *   1) 每 15 秒主动断开重连一次，让 DHCP 重来一遍，并把原因打印出来；
 *   2) 连续若干次仍拿不到，就启用静态地址兜底，保证设备在最坏情况下依然可用。
 *
 * ⚠️ 静态地址的代价必须说清楚：**它是抢地址**。如果这个地址同时被别的设备
 *    使用了，两台设备会互相打架、表现为双方都时断时续 —— 而且很难查。
 *    所以：① 地址刻意选高位（.201），尽量避开 DHCP 地址池的常用区间；
 *          ② 上电前用 ping 确认过没人占用；
 *          ③ 需要关掉时把 STATIC_FALLBACK_ENABLE 置 0 重新编译即可。
 */
#define STATIC_FALLBACK_ENABLE  1
#define STATIC_IP_FALLBACK      "10.1.41.201"
#define STATIC_GW_FALLBACK      "10.1.41.254"
#define STATIC_NETMASK_FALLBACK "255.255.255.0"
#define DEVICE_ID      "team01-esp32s3eye"

/* ============ ESP32-S3-EYE 固定接线（不用改） ============ */
#define I2C_SDA_GPIO   4      /* 与摄像头 SCCB 共用总线 */
#define I2C_SCL_GPIO   5
#define I2C_PORT       I2C_NUM_0
#define I2C_FREQ_HZ    400000
#define QMA6100P_ADDR   0x12

/* 灵敏度：±2g 量程、14位输出 下每 1g 对应的原始计数值。
 * QMA6100P 数据为 14 位左对齐（先组装 16 位再算术右移 2 位），
 * ±2g 满量程 16384 计数跨 4g => 4096 LSB/g。 */
#define QMA_SENS_LSB_PER_G  4096.0f

#define POST_INTERVAL_MS  1000

/* ===== 实测标定（旋转实验：面朝上/朝下静止两点法拟合） =====
 * 本板 QMA6100P（chip_id 0x90）零偏偏大：未标定静止模长 0.78~1.6g 漂移。
 * X 零偏 -0.553g、Y 零偏 -0.148g、Z 零偏 +0.389g、Z 灵敏度系数 0.912，
 * 标定后各静置姿态模长 0.97~1.01。换板子需重新标定。 */
#define CAL_X_OFF    2265.0f   /* 计数 = 0.553g * 4096 */
#define CAL_Y_OFF     608.0f   /* 计数 = 0.148g * 4096 */
#define CAL_Z_OFF    1594.0f   /* 计数 = 0.389g * 4096 */
#define CAL_Z_SCALE    0.912f
/* =========================================== */
/* =========================================== */

/* ============ 第3周：板载实体按键 + 本地反馈 LED ============
 *
 * 两块硬件都从原理图/官方文档确认过，不是猜的：
 *
 * 1) 4 个用户按键**不各占一个 GPIO**，而是挂在**同一个 ADC 上的电阻梯**。
 *    原理图第 4 页明确标注 `4-Keys: ADC1_CH0`，即 GPIO1。每个键按下时把
 *    分压点拉到不同电压，固件靠电压值区分按的是哪一个。
 *    （第 1 周在 IMU 型号上吃过"想当然"的亏，这次先查原理图再写码。）
 *
 * 2) 本地反馈**只能用 LED** —— 本板没有蜂鸣器，也没有可编程 RGB 灯，
 *    唯一软件可控的发光器件是绿色状态灯 "Module Power LED"，接 GPIO3。
 *
 *    ⚠️ 官方用户指南明确要求：**GPIO3 必须配置为开漏（open-drain）输出**，
 *       且不得上拉，否则可能烧掉该 LED。故本文件中 LED 只使用
 *       GPIO_MODE_OUTPUT_OD，并且永远不把它驱动为高电平。
 */
#define BTN_ADC_UNIT       ADC_UNIT_1
#define BTN_ADC_CHANNEL    ADC_CHANNEL_0   /* ESP32-S3 上 ADC1_CH0 = GPIO1 */
#define BTN_ADC_ATTEN      ADC_ATTEN_DB_12 /* 量程到 ~3.3V，覆盖最高档 2.41V */
#define BTN_POLL_MS        20              /* 轮询周期 50Hz：够快，又不费 CPU */
#define BTN_STABLE_TICKS   3               /* 连续 3 次判读一致才算按下（消抖） */

#define LED_GPIO           GPIO_NUM_3      /* 绿色 Module Power LED：开漏、拉低点亮 */

/* 第5周：录音键用 BOOT（GPIO0）。四个用户键的语义已被第3周的求助占用，
 * 再往上叠"长按录音"会改变求助的触发时机，破坏已经验过的证据链。 */
#define BOOT_BTN_GPIO      GPIO_NUM_0

/* 按键电压，取自原理图标注值。判读用"就近归并"：落在相邻两档中点之间
 * 就算该档。各档间隔最小 430mV，中点判决留出的余量足以容忍电阻误差。 */
#define BTN_MV_UP          380    /* UP+  */
#define BTN_MV_DN          820    /* DN-  */
#define BTN_MV_PLAY       1980    /* PLAY */
#define BTN_MV_MENU       2410    /* MENU */
#define BTN_MV_IDLE       3300    /* 无人按键时被上拉到 3.3V */

/* 按一次实体键采一小批 —— 与 1Hz 定时上报明显区分：
 * 网页上会看到"半秒内突然多出 5 条"，一眼能认出是人在按。 */
/* 一次按键 = 一条教学求助测试消息 + 少量环境采样。
 *
 * 采样数刻意定得很小（2 条）：求助消息是**主件**，采样只是给"回应你的人"
 * 一点连续上下文。而实测一次 HTTP 往返要 0.5~2.5 秒，采样越多，
 * 站在板子旁边的人等得越久 —— 与"按下就该立刻有反馈"直接冲突。
 * （第3周原本是 5 条，那时按键的语义还只是"采一批数据"。） */
#define HELP_CTX_SAMPLES   2
#define HELP_CTX_GAP_MS    120

static const char *TAG = "week1";
static EventGroupHandle_t s_wifi_event_group;
#define WIFI_CONNECTED_BIT BIT0

static void wifi_event_handler(void *arg, esp_event_base_t base,
                               int32_t id, void *data)
{
    if (base == WIFI_EVENT && id == WIFI_EVENT_STA_START) {
        esp_wifi_connect();
    } else if (base == WIFI_EVENT && id == WIFI_EVENT_STA_DISCONNECTED) {
        ESP_LOGW(TAG, "WiFi 断开，重试中...");
        esp_wifi_connect();
    } else if (base == IP_EVENT && id == IP_EVENT_STA_GOT_IP) {
        ip_event_got_ip_t *ev = (ip_event_got_ip_t *)data;
        ESP_LOGI(TAG, "拿到 IP: " IPSTR, IP2STR(&ev->ip_info.ip));
        xEventGroupSetBits(s_wifi_event_group, WIFI_CONNECTED_BIT);
    }
}

static void wifi_init_sta(void)
{
    s_wifi_event_group = xEventGroupCreate();
    ESP_ERROR_CHECK(esp_netif_init());
    ESP_ERROR_CHECK(esp_event_loop_create_default());
    esp_netif_create_default_wifi_sta();

    wifi_init_config_t cfg = WIFI_INIT_CONFIG_DEFAULT();
    ESP_ERROR_CHECK(esp_wifi_init(&cfg));

    ESP_ERROR_CHECK(esp_event_handler_register(WIFI_EVENT, ESP_EVENT_ANY_ID,
                                               &wifi_event_handler, NULL));
    ESP_ERROR_CHECK(esp_event_handler_register(IP_EVENT, IP_EVENT_STA_GOT_IP,
                                               &wifi_event_handler, NULL));

    wifi_config_t wifi_cfg = {0};
    strncpy((char *)wifi_cfg.sta.ssid, WIFI_SSID, sizeof(wifi_cfg.sta.ssid) - 1);
    strncpy((char *)wifi_cfg.sta.password, WIFI_PASS, sizeof(wifi_cfg.sta.password) - 1);
    wifi_cfg.sta.threshold.authmode = WIFI_AUTH_WPA2_PSK;

    ESP_ERROR_CHECK(esp_wifi_set_mode(WIFI_MODE_STA));
    ESP_ERROR_CHECK(esp_wifi_set_config(WIFI_IF_STA, &wifi_cfg));
    ESP_ERROR_CHECK(esp_wifi_start());

    /* —— 关掉 WiFi 省电（Modem-sleep）——
     * 默认省电模式下板子会周期性休眠，AP 只能先把包缓存起来，
     * 等板子醒了再发，结果就是延迟动辄几百毫秒到几秒、还伴随丢包
     * （实测：ping 板子平均 984ms、丢包 33%，而 ping 有线网关 0ms 零丢包）。
     * 数据上报是持续业务，这点电不值得省，直接关掉。 */
    ESP_ERROR_CHECK(esp_wifi_set_ps(WIFI_PS_NONE));

    ESP_LOGI(TAG, "WiFi 启动（已关闭省电模式），等待连接 %s ...", WIFI_SSID);
}

static void i2c_master_init(void)
{
    i2c_config_t conf = {
        .mode = I2C_MODE_MASTER,
        .sda_io_num = I2C_SDA_GPIO,
        .scl_io_num = I2C_SCL_GPIO,
        .sda_pullup_en = GPIO_PULLUP_ENABLE,
        .scl_pullup_en = GPIO_PULLUP_ENABLE,
        .master.clk_speed = I2C_FREQ_HZ,
    };
    ESP_ERROR_CHECK(i2c_param_config(I2C_PORT, &conf));
    ESP_ERROR_CHECK(i2c_driver_install(I2C_PORT, conf.mode, 0, 0, 0));
}

/* 启动时扫一遍 I2C 总线，把挂着的设备地址打出来 —— 首次调试定位问题用
 * 注意：探测必须用 1 字节真实读，零长度读在本驱动上会全程报
 * "i2c data read length error" 且永远探测不到设备 */
static void i2c_scan(void)
{
    ESP_LOGI(TAG, "I2C 扫描 (SDA=%d SCL=%d)...", I2C_SDA_GPIO, I2C_SCL_GPIO);
    for (uint8_t addr = 1; addr < 127; addr++) {
        uint8_t dummy;
        esp_err_t err = i2c_master_write_read_device(I2C_PORT, addr, &dummy, 0,
                                                     &dummy, 1,
                                                     pdMS_TO_TICKS(20));
        if (err == ESP_OK) {
            ESP_LOGI(TAG, "  发现设备: 0x%02x%s", addr,
                     addr == QMA6100P_ADDR ? "  <-- 加速度计(QMA6100P)" : "");
        }
    }
}

static esp_err_t qma_write_reg(uint8_t reg, uint8_t val)
{
    uint8_t buf[2] = {reg, val};
    return i2c_master_write_to_device(I2C_PORT, QMA6100P_ADDR, buf, 2,
                                      pdMS_TO_TICKS(100));
}

static esp_err_t qma_read(uint8_t reg, uint8_t *out, size_t len)
{
    return i2c_master_write_read_device(I2C_PORT, QMA6100P_ADDR, &reg, 1,
                                        out, len, pdMS_TO_TICKS(100));
}

static bool qma6100p_init(void)
{
    uint8_t chip_id = 0;
    if (qma_read(0x00, &chip_id, 1) != ESP_OK) {
        ESP_LOGE(TAG, "读不到 QMA6100P (0x%02x)，检查地址/接线", QMA6100P_ADDR);
        return false;
    }
    ESP_LOGI(TAG, "QMA6100P chip_id = 0x%02x", chip_id);
    /* 本板实测为 0x90；不同硅版本有差异（0xE7 亦见诸资料），只提示不拦 */
    if (chip_id != 0x90 && chip_id != 0xE7) {
        ESP_LOGW(TAG, "chip_id 非常见值(0x90/0xE7)，继续尝试");
    }

    /* 软复位（0x36=SOFT_RESET 写 0xB6 后回写 0x00）：
     * 实测本板上电后寄存器写入不生效、Y 轴数据冻结，软复位后恢复正常 */
    qma_write_reg(0x36, 0xB6);
    vTaskDelay(pdMS_TO_TICKS(20));
    qma_write_reg(0x36, 0x00);
    vTaskDelay(pdMS_TO_TICKS(20));

    /* 按参考驱动顺序：先退出睡眠，再设量程/带宽 */
    ESP_ERROR_CHECK(qma_write_reg(0x11, 0xC0));  /* POWER: 退出睡眠，主动模式 */
    vTaskDelay(pdMS_TO_TICKS(20));
    ESP_ERROR_CHECK(qma_write_reg(0x0F, 0x01));  /* RANGE: ±2g (QMA_RANGE_2G=0b0001) */
    ESP_ERROR_CHECK(qma_write_reg(0x10, 0x05));  /* BW: 1024Hz (合法值 0b101) */
    vTaskDelay(pdMS_TO_TICKS(50));

    /* 回读配置，确认写入是否生效 */
    uint8_t cfg[3];
    qma_read(0x0F, cfg, 3);
    ESP_LOGI(TAG, "配置回读 0F/10/11 = %02x %02x %02x (期望 01 05 C0)",
             cfg[0], cfg[1], cfg[2]);
    ESP_LOGI(TAG, "加速度计初始化完成");
    return true;
}

/* 型号鉴定：原理图/手册标 QMA7981，但官方 BSP 只有 qma6100p 组件。
 * 用三条独立证据确认实装型号：
 *   1) CHIP_ID(0x00)：QMA6100P=0x90，QMA7981=0xE7
 *   2) QMA6100P 手册特有寄存器（0x33/0x45/0x46/0x4A/0x56/0x5F）是否有有效内容
 *   3) 量程支持性：QMA6100P 支持到 ±32g(0b1111)，QMA7981 老手册只到 ±8g/±16g
 * 鉴定结束后把量程恢复为 ±2g。 */
static void imu_identify(void)
{
    uint8_t v = 0;
    ESP_LOGI(TAG, "======== 型号鉴定 ========");

    qma_read(0x00, &v, 1);
    ESP_LOGI(TAG, "ID  CHIP_ID(0x00) = 0x%02x  (0x90=QMA6100P / 0xE7=QMA7981)", v);

    uint8_t probe[]   = {0x33, 0x45, 0x46, 0x4A, 0x56, 0x5F};
    const char *nm[]  = {"NVM", "CHIP_STATE", "ULPS", "TST0_ANA", "AFE_ANA", "TST1_ANA"};
    for (int i = 0; i < 6; i++) {
        uint8_t r = 0xFF;
        esp_err_t e = qma_read(probe[i], &r, 1);
        ESP_LOGI(TAG, "REG 0x%02x %s = %s 0x%02x", probe[i], nm[i],
                 e == ESP_OK ? "ACK " : "NACK", r);
    }

    uint8_t ranges[] = {0x01, 0x02, 0x04, 0x08, 0x0F};
    const char *rn[] = {"2g", "4g", "8g", "16g", "32g"};
    for (int i = 0; i < 5; i++) {
        uint8_t rb = 0xFF;
        qma_write_reg(0x0F, ranges[i]);
        vTaskDelay(pdMS_TO_TICKS(30));
        qma_read(0x0F, &rb, 1);
        ESP_LOGI(TAG, "RANGE wr=0x%02x(+-%s) rd=0x%02x %s",
                 ranges[i], rn[i], rb, (rb == ranges[i]) ? "接受" : "拒绝/不生效");
    }
    qma_write_reg(0x0F, 0x01);
    vTaskDelay(pdMS_TO_TICKS(30));
    qma_read(0x0F, &v, 1);
    ESP_LOGI(TAG, "RANGE 恢复 = 0x%02x (期望 0x01)", v);
    ESP_LOGI(TAG, "======== 鉴定结束 ========");
}

/* ============ 第2周：上报并读回响应体 ============
 *
 * 板子只做 POST、不监听端口，所以服务器**没法主动推**指令给它。
 * 反过来利用已有的这条上行通道：服务器把待执行指令塞进 /api/data 的
 * **响应体**里，板子每次上报时顺手收下来。这样不用给板子开新端口，
 * 也不用引入 MQTT/WebSocket。
 *
 * 因此这里不能用 esp_http_client_perform()（它把响应体丢掉了），
 * 要走 open/write/fetch_headers/read 这一串手动流程，把响应读回来。
 * 响应体很小（几十~两百字节），给 512 字节缓冲足够。
 */
static esp_err_t http_post_json(const char *path, const char *json,
                                char *resp, size_t resp_sz)
{
    if (resp && resp_sz) {
        resp[0] = '\0';
    }
    char url[96];
    snprintf(url, sizeof(url), "%s%s", SERVER_BASE, path);
    esp_http_client_config_t cfg = {
        .url = url,
        .method = HTTP_METHOD_POST,
        /* 超时只给 2 秒：WiFi 差时 HTTP 会卡到超时才失败，
         * 卡多久 = 主循环停多久 = 丢多少条数据。快速失败反而更容易恢复。 */
        .timeout_ms = 2000,
    };
    esp_http_client_handle_t client = esp_http_client_init(&cfg);
    if (client == NULL) {
        return ESP_FAIL;
    }
    esp_http_client_set_header(client, "Content-Type", "application/json");

    size_t len = strlen(json);
    esp_err_t err = esp_http_client_open(client, len);
    if (err != ESP_OK) {
        ESP_LOGW(TAG, "连接失败: %s", esp_err_to_name(err));
        esp_http_client_cleanup(client);
        return err;
    }

    int written = esp_http_client_write(client, json, len);
    if (written != (int)len) {
        ESP_LOGW(TAG, "发送不完整 (%d/%d)", written, (int)len);
        esp_http_client_close(client);
        esp_http_client_cleanup(client);
        return ESP_FAIL;
    }

    int content_len = esp_http_client_fetch_headers(client);
    int status = esp_http_client_get_status_code(client);
    if (status != 200) {
        ESP_LOGW(TAG, "上报失败 HTTP %d", status);
        esp_http_client_close(client);
        esp_http_client_cleanup(client);
        return ESP_FAIL;
    }

    /* 把响应体读回来（可能带下行指令）*/
    int n = 0;
    if (resp && resp_sz > 1 && content_len > 0) {
        while (n < (int)resp_sz - 1) {
            int r = esp_http_client_read(client, resp + n, resp_sz - 1 - n);
            if (r <= 0) {
                break;
            }
            n += r;
        }
        resp[n] = '\0';
    }
    esp_http_client_close(client);
    esp_http_client_cleanup(client);
    return ESP_OK;
}

/* ---- 第5周：上传一段二进制（WAV）----
 *
 * 与 http_post_json 的区别只有两点，但都必须说清楚：
 *   1) Content-Type 是 audio/wav，不是 application/json；
 *   2) 超时给 15 秒（JSON 只给 2 秒）。因为服务端收到这段音频后要跑一遍
 *      离线识别再回答，2 秒不够 —— 用 JSON 的超时会把"正在识别"误判成"发送失败"。
 *      代价是：这条路径最长会把主循环占住 15 秒。只在录音结束时走一次，可以接受。
 */
static esp_err_t http_post_binary(const char *path, const uint8_t *data, size_t len,
                                  char *resp, size_t resp_sz)
{
    if (resp && resp_sz) {
        resp[0] = '\0';
    }
    char url[96];
    snprintf(url, sizeof(url), "%s%s", SERVER_BASE, path);
    esp_http_client_config_t cfg = {
        .url = url,
        .method = HTTP_METHOD_POST,
        .timeout_ms = 15000,
    };
    esp_http_client_handle_t client = esp_http_client_init(&cfg);
    if (client == NULL) {
        return ESP_FAIL;
    }
    esp_http_client_set_header(client, "Content-Type", "audio/wav");

    esp_err_t err = esp_http_client_open(client, len);
    if (err != ESP_OK) {
        ESP_LOGW(TAG, "音频上传连接失败: %s", esp_err_to_name(err));
        esp_http_client_cleanup(client);
        return err;
    }
    int written = esp_http_client_write(client, (const char *)data, len);
    if (written != (int)len) {
        ESP_LOGW(TAG, "音频发送不完整 (%d/%d)", written, (int)len);
        esp_http_client_close(client);
        esp_http_client_cleanup(client);
        return ESP_FAIL;
    }

    int content_len = esp_http_client_fetch_headers(client);
    int status = esp_http_client_get_status_code(client);
    int n = 0;
    if (resp && resp_sz > 1 && content_len > 0) {
        while (n < (int)resp_sz - 1) {
            int r = esp_http_client_read(client, resp + n, resp_sz - 1 - n);
            if (r <= 0) {
                break;
            }
            n += r;
        }
        resp[n] = '\0';
    }
    esp_http_client_close(client);
    esp_http_client_cleanup(client);
    if (status != 200) {
        ESP_LOGW(TAG, "音频上传失败 HTTP %d", status);
        return ESP_FAIL;
    }
    return ESP_OK;
}

/* ---------- 第2周：从响应体里解出下行指令 ----------
 * 用 IDF 自带的 cJSON（组件名 json，默认就在构建里，不需要写 REQUIRES）。
 * 期望的响应形如：
 *   {"ok":true,"id":123,"server_ms":...,"command":{"request_id":"CMD-20260918-0001",
 *     "type":"recollect","params":{"samples":8,"interval_ms":120}}}
 */
typedef struct {
    char request_id[48];
    char type[16];      /* "recollect" / "pause" / "resume" / "help_reply" */
    int  samples;
    int  interval_ms;
    /* help_reply 专用：哪条求助、被做了什么动作（ack / cancel）。
     * 不复用 samples/interval_ms 是因为语义完全不同 —— 那两个字段在这里
     * 恒为默认值，混用会让读代码的人以为有一条采集指令。 */
    char event_id[48];
    char action[12];
} cmd_t;

static bool parse_command(const char *resp, cmd_t *out)
{
    if (!resp || !resp[0]) {
        return false;
    }
    cJSON *root = cJSON_Parse(resp);
    if (root == NULL) {
        return false;
    }
    bool found = false;
    cJSON *cmd = cJSON_GetObjectItem(root, "command");
    if (cJSON_IsObject(cmd)) {
        cJSON *id = cJSON_GetObjectItem(cmd, "request_id");
        if (cJSON_IsString(id) && id->valuestring && id->valuestring[0]) {
            snprintf(out->request_id, sizeof(out->request_id), "%s",
                     id->valuestring);
            out->samples = 10;
            out->interval_ms = 100;
            /* 类型缺失时按 recollect 处理（向后兼容第2周的服务端响应） */
            cJSON *ty = cJSON_GetObjectItem(cmd, "type");
            snprintf(out->type, sizeof(out->type), "%s",
                     (cJSON_IsString(ty) && ty->valuestring && ty->valuestring[0])
                         ? ty->valuestring : "recollect");
            cJSON *params = cJSON_GetObjectItem(cmd, "params");
            if (cJSON_IsObject(params)) {
                cJSON *s  = cJSON_GetObjectItem(params, "samples");
                cJSON *iv = cJSON_GetObjectItem(params, "interval_ms");
                if (cJSON_IsNumber(s) && s->valueint > 0) {
                    out->samples = s->valueint;
                }
                if (cJSON_IsNumber(iv) && iv->valueint > 0) {
                    out->interval_ms = iv->valueint;
                }
                /* help_reply 的参数 */
                cJSON *eid = cJSON_GetObjectItem(params, "event_id");
                if (cJSON_IsString(eid) && eid->valuestring) {
                    snprintf(out->event_id, sizeof(out->event_id), "%s",
                             eid->valuestring);
                }
                cJSON *act = cJSON_GetObjectItem(params, "action");
                if (cJSON_IsString(act) && act->valuestring) {
                    snprintf(out->action, sizeof(out->action), "%s",
                             act->valuestring);
                }
            }
            found = true;
        }
    }
    cJSON_Delete(root);
    if (!found) {
        return false;
    }
    /* 兜底限幅：服务器也会限，但板子自己再挡一层，
     * 免得异常参数把主循环卡死（比如 samples=100000） */
    if (out->samples > 200)        out->samples = 200;
    if (out->interval_ms < 20)     out->interval_ms = 20;
    if (out->interval_ms > 2000)   out->interval_ms = 2000;
    return true;
}

/* QMA6100P 14位数据组装：
 * MSB 寄存器 = DX[13:6]，LSB 寄存器 bit5:0 = DX[5:0]，bit7 = NEWDATA 标志
 * 14位二进制补码，bit13 为符号位 */
static inline int16_t qma_assemble14(uint8_t lsb, uint8_t msb)
{
    int16_t v = (int16_t)(((uint16_t)msb << 6) | (lsb & 0x3F));
    if (v & 0x2000) {
        v -= 0x4000;
    }
    return v;
}

/* ================= 设备自身状态：芯片温度 + 堆内存 =================
 *
 * 温度：ESP32-S3 芯片内部自带一颗温度传感器，不用外接任何元件。
 *   注意它测的是**芯片裸片温度**，不是环境温度——芯片自己会发热，
 *   通常比室温高几度。适合观察"板子负载/温升趋势"，不能当室温计用。
 *
 * 内存：esp_get_free_heap_size() 返回当前可用堆（字节），
 *   esp_get_minimum_free_heap_size() 返回开机以来的历史最低值，
 *   后者是判断"有没有内存泄漏"的关键：它随时间持续下降 = 有泄漏。
 *   总堆用 heap_caps_get_total_size(MALLOC_CAP_DEFAULT)——必须与上面
 *   两个函数**同口径**（它们内部都是 MALLOC_CAP_DEFAULT），否则
 *   "总-剩余"算出来的已用值会离谱。ESP32-S3-EYE 带 8MB PSRAM，
 *   所以这里总量约 8MB 而不是内部 RAM 的几百 KB。
 *
 * 这两个量与 IMU 一起上报，网页上就能同时看到"被测对象的物理量"
 * 和"设备自身的健康度"。初始化失败不影响主链路，只是字段报 null。
 * ================================================================ */
static temperature_sensor_handle_t s_tsens = NULL;

static void temp_sensor_init(void)
{
    /* 量程必须完整落在芯片预定义档位内，否则 install 会报
     * "Out of testing range" + ESP_ERR_INVALID_ARG（实测踩过）。
     * ESP32-S3 只有 5 档：50~125 / 20~100 / -10~80 / -30~50 / -40~20，
     * 其中 -10~80 误差最小(±1°C)，且完全覆盖芯片工作温区(通常 40~60°C)。 */
    temperature_sensor_config_t cfg = TEMPERATURE_SENSOR_CONFIG_DEFAULT(-10, 80);
    esp_err_t err = temperature_sensor_install(&cfg, &s_tsens);
    if (err != ESP_OK) {
        ESP_LOGW(TAG, "温度传感器 install 失败: %s (%d), temp_c 将上报 null",
                 esp_err_to_name(err), err);
        s_tsens = NULL;
        return;
    }
    err = temperature_sensor_enable(s_tsens);
    if (err != ESP_OK) {
        ESP_LOGW(TAG, "温度传感器 enable 失败: %s (%d), temp_c 将上报 null",
                 esp_err_to_name(err), err);
        s_tsens = NULL;
        return;
    }
    float t = 0;
    err = temperature_sensor_get_celsius(s_tsens, &t);
    ESP_LOGI(TAG, "温度传感器就绪，当前 %.1f C (读取返回 %s)", t, esp_err_to_name(err));
}

/* ==================== 采样一次并组装 JSON ====================
 *
 * 第2周把这段从主循环里抽出来，因为它有了多种触发方式；第3周又多了"按键"。
 * 触发方式现在有三种：
 *   · 定时上报        request_id=NULL,      trigger="timer"
 *   · 网页下发指令触发 request_id="CMD-..",  trigger="command", cmd_state="received"/"done"
 *   · 板载实体按键触发 request_id="BTN-..",  trigger="button",  button="MENU"/"PLAY"/"UP+"/"DN-"
 *
 * 服务器靠 request_id + trigger + button 就能证明"这一条确实是被那次动作
 * 触发的"，而不是从历史里翻出来的旧数据 —— 这是第2、3周共同的核心。
 *
 * 参数包成结构体：第2周是 3 个字符串参数，第3周要再加一个，继续加位置参数
 * 很容易把顺序写错（而且编译期不报错）。结构体让调用点自解释。
 *
 * 返回写好的 JSON 长度；<=0 表示失败。
 */
typedef struct {
    const char *request_id;   /* 触发批次 id：CMD-…（网页）/ BTN-…（按键）/ NULL（定时） */
    const char *trigger;      /* "timer" / "command" / "button" */
    const char *cmd_state;    /* 指令回执阶段；按键与定时上报为 NULL */
    const char *button;       /* 实体按键名；非按键触发的批次为 NULL */
    const char *cmd_note;     /* 回执备注：失败原因，或"部分送达"的说明 */
    int press_delay_ms;       /* 按下 → 本条采样组装完成 的毫秒数；-1 = 不适用 */
} sample_ctx_t;

/* 读一次加速度并换算成 g。
 * 抽出来是因为现在有两个调用方：常规采样组装 JSON，以及求助消息里的
 * 环境快照。两处一旦各写一份，标定值改了一处、忘了另一处，就会得到
 * 两个互相矛盾的数字 —— 而它们看起来都"像真的"。 */
static bool read_accel_g(float *ax, float *ay, float *az, int16_t raw_out[3])
{
    uint8_t raw[6];
    if (qma_read(0x01, raw, sizeof(raw)) != ESP_OK) {
        return false;
    }
    /* QMA6100P：0x01 起 6 字节 = [DXL,DXM,DYL,DYM,DZL,DZM]，LSB 高位含 NEWDATA 标志 */
    int16_t ax_raw = qma_assemble14(raw[0], raw[1]);
    int16_t ay_raw = qma_assemble14(raw[2], raw[3]);
    int16_t az_raw = qma_assemble14(raw[4], raw[5]);
    *ax = (ax_raw + CAL_X_OFF) / QMA_SENS_LSB_PER_G;
    *ay = (ay_raw + CAL_Y_OFF) / QMA_SENS_LSB_PER_G;
    *az = (az_raw - CAL_Z_OFF) / (QMA_SENS_LSB_PER_G * CAL_Z_SCALE);
    if (raw_out) {
        /* 原始寄存器值留给采样那条日志用（第1周就是靠它确认 14 位组装是对的），
         * 求助消息的快照不需要，传 NULL 即可。 */
        raw_out[0] = ax_raw;
        raw_out[1] = ay_raw;
        raw_out[2] = az_raw;
    }
    return true;
}

static int build_payload(char *out, size_t out_sz, uint32_t seq,
                         const sample_ctx_t *ctx)
{
    const char *request_id = ctx ? ctx->request_id : NULL;
    const char *trigger    = ctx ? ctx->trigger    : NULL;
    const char *cmd_state  = ctx ? ctx->cmd_state  : NULL;
    const char *button     = ctx ? ctx->button     : NULL;
    const char *cmd_note   = ctx ? ctx->cmd_note   : NULL;
    int press_delay_ms     = ctx ? ctx->press_delay_ms : -1;
    float ax, ay, az;
    int16_t raw_v[3];
    if (!read_accel_g(&ax, &ay, &az, raw_v)) {
        ESP_LOGE(TAG, "读取加速度计数据寄存器(0x01)失败");
        return -1;
    }

    /* —— 读设备自身状态：芯片温度 + 堆内存 ——
     * 温度读不到时上报 JSON 的 null（而不是 0），免得网页显示 "0 °C"
     * 这种看起来像真的、其实是假的读数。 */
    float temp_c = 0.0f;
    char tbuf[16];
    bool temp_ok = (s_tsens != NULL) &&
                   (temperature_sensor_get_celsius(s_tsens, &temp_c) == ESP_OK);
    snprintf(tbuf, sizeof(tbuf), temp_ok ? "%.1f" : "null", temp_c);

    uint32_t free_heap  = esp_get_free_heap_size();
    uint32_t min_free   = esp_get_minimum_free_heap_size();
    uint32_t total_heap = heap_caps_get_total_size(MALLOC_CAP_DEFAULT);

    /* —— WiFi 信号强度 RSSI（dBm，越接近 0 越好）——
     * 数据断流时第一个该看的就是它：-60 以上很好，-70 尚可，
     * -80 以下基本必然丢包。信号差要先改善物理位置，改代码没用。 */
    wifi_ap_record_t ap = {0};
    char rbuf[8];
    bool rssi_ok = (esp_wifi_sta_get_ap_info(&ap) == ESP_OK);
    snprintf(rbuf, sizeof(rbuf), rssi_ok ? "%d" : "null", (int)ap.rssi);

    /* 第2/3周：触发来源相关字段。没有就用 null，不用空字符串——
     * 空字符串在 SQL 里不好区分"没有"和"空"。 */
    char rid[56];
    char cst[28];
    char btn[12];
    char pdl[16];
    if (request_id && request_id[0]) {
        snprintf(rid, sizeof(rid), "\"%s\"", request_id);
    } else {
        snprintf(rid, sizeof(rid), "null");
    }
    if (cmd_state && cmd_state[0]) {
        snprintf(cst, sizeof(cst), "\"%s\"", cmd_state);
    } else {
        snprintf(cst, sizeof(cst), "null");
    }
    if (button && button[0]) {
        snprintf(btn, sizeof(btn), "\"%s\"", button);
    } else {
        snprintf(btn, sizeof(btn), "null");
    }
    if (press_delay_ms >= 0) {
        snprintf(pdl, sizeof(pdl), "%d", press_delay_ms);
    } else {
        snprintf(pdl, sizeof(pdl), "null");
    }
    /* 回执备注：失败原因 / "部分送达"的说明。没有就用 JSON null ——
     * 空字符串在 SQL 里分不清"没有备注"和"备注是空的"。 */
    char note[80];
    if (cmd_note && cmd_note[0]) {
        snprintf(note, sizeof(note), "\"%s\"", cmd_note);
    } else {
        snprintf(note, sizeof(note), "null");
    }

    int n = snprintf(out, out_sz,
                     "{\"device_id\":\"%s\",\"seq\":%lu,\"ts\":%lld,"
                     "\"ax\":%.4f,\"ay\":%.4f,\"az\":%.4f,"
                     "\"gx\":0.00,\"gy\":0.00,\"gz\":0.00,"
                     "\"temp_c\":%s,\"free_heap\":%lu,\"min_free_heap\":%lu,"
                     "\"total_heap\":%lu,\"rssi\":%s,"
                     "\"request_id\":%s,\"trigger\":\"%s\",\"cmd_state\":%s,"
                     "\"cmd_note\":%s,"
                     "\"button\":%s,\"press_delay_ms\":%s}",
                     DEVICE_ID, (unsigned long)seq,
                     (long long)(esp_timer_get_time() / 1000),
                     ax, ay, az,
                     tbuf,
                     (unsigned long)free_heap, (unsigned long)min_free,
                     (unsigned long)total_heap, rbuf,
                     rid, trigger ? trigger : "timer", cst, note, btn, pdl);

    float norm = sqrtf(ax * ax + ay * ay + az * az);
    ESP_LOGI(TAG, "raw=[%d %d %d] ax=%.3f ay=%.3f az=%.3f |a|=%.3f"
                  " | %sC 信号%sdBm 堆 %.2fMB(已用%.2f/共%.2f, 最低%.2fMB)"
                  " | %s%s",
             raw_v[0], raw_v[1], raw_v[2], ax, ay, az, norm,
             tbuf, rbuf,
             free_heap / 1048576.0,
             (total_heap - free_heap) / 1048576.0,
             total_heap / 1048576.0,
             min_free / 1048576.0,
             (request_id && request_id[0]) ? request_id : "timer",
             (cmd_state && cmd_state[0]) ? " <-- 回执" : "");

    if (n <= 0 || n >= (int)out_sz) {
        ESP_LOGE(TAG, "payload 缓冲不足 (%d >= %d)", n, (int)out_sz);
        return -1;
    }
    return n;
}

/* ==================== 第2周：执行一次「重新采集」指令 ====================
 *
 * 流程（对应服务器侧的四段证据链）：
 *   1) 先回报 received —— 让服务器知道"设备确实收到并开始干了"
 *   2) 按服务器给的 samples / interval_ms 连续采一批，每条都带 request_id
 *   3) 最后一条带 cmd_state="done" —— 服务器据此判定完成
 *
 * 注意：这一批数据的 trigger 都是 "command"，与 1Hz 定时上报区分开，
 * 网页因此能把"这次新采的"高亮出来，而不是混在历史里。
 */
static void run_command(const cmd_t *cmd, uint32_t *seq)
{
    char payload[512];
    char resp[512];

    ESP_LOGI(TAG, "======== 收到下行指令 %s：采集 %d 条，间隔 %d ms ========",
             cmd->request_id, cmd->samples, cmd->interval_ms);

    /* 1) 立刻回报 received */
    sample_ctx_t c = { .request_id = cmd->request_id, .trigger = "command",
                       .cmd_state = "received", .button = NULL,
                       .cmd_note = NULL, .press_delay_ms = -1 };
    int n = build_payload(payload, sizeof(payload), (*seq)++, &c);
    if (n > 0 && http_post_json(PATH_DATA, payload, resp, sizeof(resp)) == ESP_OK) {
        ESP_LOGI(TAG, "  [1/3] 已回报 received");
    } else {
        ESP_LOGW(TAG, "  [1/3] received 回报失败，仍继续采集");
    }

    /* 2) 执行采集。数两个数：
     *      built —— 真正采到的条数（IMU 读失败就采不到）
     *      ok    —— 真正送到服务器的条数
     *    两个数分开数，才能分清是"采不到"还是"送不出去" ——
     *    这两种故障要查的地方完全不同（前者查 I2C/传感器，后者查网络）。
     */
    int built = 0, ok = 0;
    for (int i = 0; i < cmd->samples; i++) {
        if (i > 0) {
            vTaskDelay(pdMS_TO_TICKS(cmd->interval_ms));
        }
        c.cmd_state = NULL;           /* 样本本身不带结论 */
        n = build_payload(payload, sizeof(payload), (*seq)++, &c);
        if (n <= 0) {
            ESP_LOGW(TAG, "  [2/3] 采集 %d/%d 失败（读不到传感器）",
                     i + 1, cmd->samples);
            continue;
        }
        built++;
        if (http_post_json(PATH_DATA, payload, resp, sizeof(resp)) == ESP_OK) {
            ok++;
            ESP_LOGI(TAG, "  [2/3] 采集 %d/%d 已送达", i + 1, cmd->samples);
        } else {
            ESP_LOGW(TAG, "  [2/3] 采集 %d/%d 上报失败", i + 1, cmd->samples);
        }
    }

    /* 3) 独立回报**结论**（done / failed + 原因），最多重试 3 次。
     *
     * 为什么结论要单独一条、而不是搭在最后一条样本上：
     * 那样只有在"最后一条恰好成功"时结论才送得出去。一旦整批都被拒绝，
     * 服务器就永远收不到 done，只能一路等到 exec 超时 —— 于是"设备明确
     * 告诉你它失败了"和"服务器什么都没听到"被混成同一件事，这恰恰是
     * failed 与 timeout 该分开的意义所在。
     */
    char note[80];
    const char *verdict;
    if (built == 0) {
        verdict = "failed";
        snprintf(note, sizeof(note), "采样全部失败：%d 次都没读到传感器", cmd->samples);
    } else if (ok == 0) {
        verdict = "failed";
        snprintf(note, sizeof(note), "%d 条全部上报失败（网络或服务器不可达）", built);
    } else if (ok < cmd->samples) {
        verdict = "done";       /* 完成就是完成，不降级；但代价必须写下来 */
        snprintf(note, sizeof(note), "部分送达：%d/%d 条成功", ok, cmd->samples);
    } else {
        verdict = "done";
        note[0] = '\0';
    }

    sample_ctx_t v = { .request_id = cmd->request_id, .trigger = "command",
                       .cmd_state = verdict, .button = NULL,
                       .cmd_note = note[0] ? note : NULL, .press_delay_ms = -1 };
    for (int t = 0; t < 3; t++) {
        if (t > 0) {
            vTaskDelay(pdMS_TO_TICKS(600));
        }
        n = build_payload(payload, sizeof(payload), (*seq)++, &v);
        if (n > 0 && http_post_json(PATH_DATA, payload, resp, sizeof(resp)) == ESP_OK) {
            ESP_LOGI(TAG, "  [3/3] 结论已回报：%s%s%s",
                     verdict, note[0] ? " · " : "", note[0] ? note : "");
            break;
        }
        ESP_LOGW(TAG, "  [3/3] 结论回报失败（第 %d/3 次）", t + 1);
    }
    ESP_LOGI(TAG, "  指令 %s 执行结束：采到 %d/%d，送达 %d",
             cmd->request_id, built, cmd->samples, ok);
}

/* ==================== 第3周：本地反馈 LED ====================
 *
 * LED 不在按键任务里直接做延时闪灯 —— 那样按键任务会被 vTaskDelay 卡住，
 * 期间的按键全丢。改成"投递灯效"：
 *     任何任务 → led_signal(灯效) → 队列 → led_task 串行播放
 * 调用方立刻返回；多个灯效请求也不会互相交错打乱节奏。
 */
typedef enum {
    LED_PAT_OFF = 0,
    LED_PAT_BOOT,       /* 上电自检：连闪 3 次 —— 用眼睛确认灯是活的、极性是对的 */
    LED_PAT_KEY,        /* 按到键：短亮一下 —— "我收到了" */
    LED_PAT_UPLOAD_OK,  /* 整批上传成功：连闪 2 次 —— "服务器收到了" */
    LED_PAT_FAIL,       /* 有上报失败：快闪 5 次 —— "没送出去" */
    LED_PAT_REPLY_ACK,  /* 远端有人回应：慢闪 4 次 —— "有人理我了"（任务卡：迁移确认机制） */
    LED_PAT_REPLY_CANCEL, /* 求助被取消：长亮 1 秒 —— "这事结束了"（任务卡：迁移取消机制） */
} led_pattern_t;

static QueueHandle_t s_led_q;

/* ⚠️ 开漏输出 + 拉低点亮。绝不驱动为高电平（官方指南：上拉可能烧 LED）。 */
static void led_raw(bool on)
{
    gpio_set_level(LED_GPIO, on ? 0 : 1);
}

static void led_play(led_pattern_t p)
{
    switch (p) {
    case LED_PAT_BOOT:
        for (int i = 0; i < 3; i++) {
            led_raw(true);  vTaskDelay(pdMS_TO_TICKS(150));
            led_raw(false); vTaskDelay(pdMS_TO_TICKS(150));
        }
        break;
    case LED_PAT_KEY:
        led_raw(true);  vTaskDelay(pdMS_TO_TICKS(60));
        led_raw(false);
        break;
    case LED_PAT_UPLOAD_OK:
        for (int i = 0; i < 2; i++) {
            led_raw(true);  vTaskDelay(pdMS_TO_TICKS(120));
            led_raw(false); vTaskDelay(pdMS_TO_TICKS(120));
        }
        break;
    case LED_PAT_FAIL:
        for (int i = 0; i < 5; i++) {
            led_raw(true);  vTaskDelay(pdMS_TO_TICKS(60));
            led_raw(false); vTaskDelay(pdMS_TO_TICKS(60));
        }
        break;
    /* 两个"远端反馈"灯效刻意与本地灯效拉开差别：
     * 本地确认是"快而短"（60ms），远端回应是"慢而长"（350ms×4）——
     * 站在板子旁边的人在余光里也能分清"是我按到了"还是"有人回我了"。
     * 如果两者长得像，这个反馈就等于没有。 */
    case LED_PAT_REPLY_ACK:
        for (int i = 0; i < 4; i++) {
            led_raw(true);  vTaskDelay(pdMS_TO_TICKS(350));
            led_raw(false); vTaskDelay(pdMS_TO_TICKS(200));
        }
        break;
    case LED_PAT_REPLY_CANCEL:
        led_raw(true);  vTaskDelay(pdMS_TO_TICKS(1000));
        led_raw(false);
        break;
    case LED_PAT_OFF:
    default:
        led_raw(false);
        break;
    }
}

static void led_task(void *arg)
{
    led_pattern_t p;
    while (1) {
        if (xQueueReceive(s_led_q, &p, portMAX_DELAY) == pdTRUE) {
            led_play(p);
        }
    }
}

static void led_signal(led_pattern_t p)
{
    if (s_led_q) {
        /* 队列满就丢掉这一条：灯只是提示，不该阻塞上报链路 */
        xQueueSend(s_led_q, &p, 0);
    }
}

static void led_init(void)
{
    gpio_config_t cfg = {
        .pin_bit_mask = 1ULL << LED_GPIO,
        .mode         = GPIO_MODE_OUTPUT_OD,   /* 开漏，见上面的 ⚠️ */
        .pull_up_en   = GPIO_PULLUP_DISABLE,   /* 绝不上拉 */
        .pull_down_en = GPIO_PULLDOWN_DISABLE,
        .intr_type    = GPIO_INTR_DISABLE,
    };
    gpio_config(&cfg);
    led_raw(false);                            /* 上电先熄灭 */

    s_led_q = xQueueCreate(8, sizeof(led_pattern_t));
    xTaskCreate(led_task, "led", 2048, NULL, 4, NULL);
}

/* ==================== 第3周：板载实体按键（ADC 电阻梯） ====================
 *
 * 四个键共用 GPIO1，只能靠电压区分。固件要做两件事：
 *   1) 把读到的电压归到最近的档位
 *   2) 消抖，并且**只在「抬起→按下」的边沿触发一次**（长按不会连续触发）
 */
typedef enum {
    BTN_NONE = 0,
    BTN_UP, BTN_DN, BTN_PLAY, BTN_MENU,
} btn_id_t;

static const char *btn_name(btn_id_t b)
{
    switch (b) {
    case BTN_UP:   return "UP+";
    case BTN_DN:   return "DN-";
    case BTN_PLAY: return "PLAY";
    case BTN_MENU: return "MENU";
    default:       return NULL;
    }
}

static adc_oneshot_unit_handle_t s_adc  = NULL;
static adc_cali_handle_t         s_cali = NULL;

/* 队列里传的不只是"哪个键"，还带上**按下那一刻的设备时刻**。
 * 有了它才能算出「按下 → 首条数据组装完成」这段延迟，也就是按键响应速度。 */
typedef struct {
    btn_id_t btn;
    int64_t  press_ms;      /* esp_timer_get_time()/1000，按下瞬间 */
} btn_event_t;

static QueueHandle_t s_btn_q;

/* 电压(mV) → 按键：用相邻两档的中点做判决边界 */
static btn_id_t btn_classify(int mv)
{
    if (mv < (BTN_MV_UP   + BTN_MV_DN)   / 2) return BTN_UP;
    if (mv < (BTN_MV_DN   + BTN_MV_PLAY) / 2) return BTN_DN;
    if (mv < (BTN_MV_PLAY + BTN_MV_MENU) / 2) return BTN_PLAY;
    if (mv < (BTN_MV_MENU + BTN_MV_IDLE) / 2) return BTN_MENU;
    return BTN_NONE;
}

static bool btn_read_mv(int *out_mv)
{
    int raw = 0;
    if (s_adc == NULL || adc_oneshot_read(s_adc, BTN_ADC_CHANNEL, &raw) != ESP_OK) {
        return false;
    }
    if (s_cali) {
        int mv = 0;
        if (adc_cali_raw_to_voltage(s_cali, raw, &mv) == ESP_OK) {
            *out_mv = mv;
            return true;
        }
    }
    /* 没有校准方案时的退路：按 12bit / 3.3V 满量程线性换算。
     * 精度差一些，但按键各档之间差几百 mV，足够用。 */
    *out_mv = raw * 3300 / 4095;
    return true;
}

static void btn_init(void)
{
    adc_oneshot_unit_init_cfg_t ucfg = { .unit_id = BTN_ADC_UNIT };
    ESP_ERROR_CHECK(adc_oneshot_new_unit(&ucfg, &s_adc));

    adc_oneshot_chan_cfg_t ccfg = {
        .atten    = BTN_ADC_ATTEN,
        .bitwidth = ADC_BITWIDTH_DEFAULT,
    };
    ESP_ERROR_CHECK(adc_oneshot_config_channel(s_adc, BTN_ADC_CHANNEL, &ccfg));

#if ADC_CALI_SCHEME_CURVE_FITTING_SUPPORTED
    adc_cali_curve_fitting_config_t cal = {
        .unit_id  = BTN_ADC_UNIT,
        .chan     = BTN_ADC_CHANNEL,
        .atten    = BTN_ADC_ATTEN,
        .bitwidth = ADC_BITWIDTH_DEFAULT,
    };
    if (adc_cali_create_scheme_curve_fitting(&cal, &s_cali) != ESP_OK) {
        s_cali = NULL;
    }
#endif

    s_btn_q = xQueueCreate(4, sizeof(btn_event_t));

    int mv = 0;
    btn_id_t cur = btn_read_mv(&mv) ? btn_classify(mv) : BTN_NONE;
    ESP_LOGI(TAG, "按键阵列就绪（ADC1_CH0 = GPIO1，电压校准 %s），"
                  "空闲读数 %d mV → 判读 %s",
             s_cali ? "已启用" : "未启用（退化为线性换算）",
             mv, btn_name(cur) ? btn_name(cur) : "无按键");
}

static void btn_task(void *arg)
{
    btn_id_t stable = BTN_NONE;   /* 消抖后确认的状态 */
    btn_id_t cand   = BTN_NONE;   /* 正在观察的候选状态 */
    int      agree  = 0;          /* 连续一致的次数 */

    while (1) {
        int mv = 0;
        btn_id_t now = btn_read_mv(&mv) ? btn_classify(mv) : BTN_NONE;

        if (now == cand) {
            if (agree < BTN_STABLE_TICKS) agree++;
        } else {
            cand  = now;
            agree = 0;
        }

        if (agree >= BTN_STABLE_TICKS && cand != stable) {
            stable = cand;
            if (stable != BTN_NONE) {
                ESP_LOGI(TAG, ">>> 实体按键按下: %s (%d mV)", btn_name(stable), mv);
                led_signal(LED_PAT_KEY);           /* 本地反馈：立刻告诉用户"按到了" */
                btn_event_t ev = { .btn = stable,
                                   .press_ms = esp_timer_get_time() / 1000 };
                xQueueSend(s_btn_q, &ev, 0);       /* 采集交给主循环做 */
            }
        }
        vTaskDelay(pdMS_TO_TICKS(BTN_POLL_MS));
    }
}

/* ==================== 第3周：执行一次「实体按键采集」 ====================
 *
 * 与网页下发指令的区别：
 *   · 指令是**别人**让设备采：服务器受理 → 下发 → 设备接收 → 完成，四段可查
 *   · 按键是**现场的人**让设备采：没有下行通道，所以自然没有前两段
 * 共同点是都产生一批带 request_id 的新数据，网页据此证明"确实新采了"。
 *
 * 返回 true = 这一批全部送达（决定本地 LED 给成功还是失败提示）。
 */
/* ==================== 第3周：按键发起「教学求助测试消息」 ====================
 *
 * ⚠️ 安全边界（写在代码里，不只写在文档里）：
 *    本工程只连接**本组教学接收端**（SERVER_BASE 指向本组服务器）。
 *    它没有任何外呼能力 —— 不打电话、不发短信、不推任何第三方服务。
 *    按键唯一的动作是往本组服务器写一条记录，并在网页上显示出来。
 *
 * 任务卡第3周问的是："佩戴者按下按键，**自己和查看信息的人**分别应获得什么反馈？"
 * 所以这个函数要同时照顾两边：
 *
 *   站在板子旁边的人（自己）        远端看网页的人（查看信息的人）
 *   ────────────────────────        ────────────────────────────
 *   按下的瞬间：绿灯短亮一下          （还没有，要先过网络）
 *   消息送达后：绿灯连闪 2 次         网页上出现这条求助
 *   有人回应后：绿灯慢闪 4 次         网页上显示"某某已回应"
 *   被取消后  ：绿灯长亮 1 秒         网页上显示"已取消"
 *
 *   左边三个都是**本地**反馈（最后两个要等远端回传，但显示在本地）；
 *   右边三个都要求**真的发生过**才算 —— 这就是三级反馈必须分开的原因。
 *
 * 返回值 = 求助消息是否送达服务器。它决定灯效，也决定本地是否要提示失败。
 */
static bool run_help_request(const btn_event_t *ev, uint32_t *seq)
{
    char payload[512];
    char resp[512];
    static uint32_t s_btn_n = 0;
    static uint32_t s_boot_nonce = 0;

    /* 事件 id 必须**跨重启唯一**。
     * 教训：第一版只用内存里的计数器（BTN-MENU-0001）。板子一重启计数器归零，
     * 两次不同时间按的 MENU 生成同一个 id，服务器按 id 聚合时把两次按合成了
     * 一条 —— 样本数 10、跨度 105 秒。看着像证据，其实是错的，这比没有证据更糟。
     * 所以加一个上电随机数做前缀：id 形如 BTN-1a2b3c-MENU-0001。
     * 这里刻意不用日期时间 —— 板子没有 RTC，绝对时间统一由服务器提供。 */
    if (s_boot_nonce == 0) {
        s_boot_nonce = esp_random() & 0xFFFFFF;   /* 24 位，撞车概率 ~1/1.7e7 */
    }

    const char *bname = btn_name(ev->btn);
    char evid[40];
    snprintf(evid, sizeof(evid), "BTN-%06lx-%s-%04lu",
             (unsigned long)s_boot_nonce, bname, (unsigned long)(++s_btn_n));

    int press_delay = (int)(esp_timer_get_time() / 1000 - ev->press_ms);

    ESP_LOGI(TAG, "======== 教学求助测试消息 %s（按键 %s）========",
             evid, bname);
    ESP_LOGI(TAG, "  仅发往本组教学接收端，不连接任何真实紧急服务");

    /* 1) 环境快照 —— 给回应的人一点上下文：板子当时什么姿态、多热。
     *    读不到 IMU 也照样发求助：求助本身比快照重要得多，
     *    不能因为传感器读不到就把"有人需要帮忙"这件事吞掉。
     *    （对比第2周的采集指令：那里读不到就如实回报失败，因为采不到数据
     *      就是没完成；两件事的价值排序不同。） */
    float ax = 0.0f, ay = 0.0f, az = 0.0f;
    bool imu_ok = read_accel_g(&ax, &ay, &az, NULL);
    float norm = imu_ok ? sqrtf(ax * ax + ay * ay + az * az) : 0.0f;

    char snap[220];
    snprintf(snap, sizeof(snap),
             "{\"imu_ok\":%s,\"ax\":%.4f,\"ay\":%.4f,\"az\":%.4f,"
             "\"norm_g\":%.4f,\"press_delay_ms\":%d}",
             imu_ok ? "true" : "false", ax, ay, az, norm, press_delay);

    /* 2) 主件：把求助消息发给服务器。
     *    local_feedback="led" 是**设备自报**：板子确实已经给过本地反馈了
     *    （灯效在 btn_task 里按下瞬间就点了，与网络无关）。
     *    服务器据此才能把"本地确认"和"VPS 接收"当两件事分别展示 ——
     *    如果不上报这个字段，服务器就只能假设灯亮过，那是编的。 */
    char body[460];
    int n = snprintf(body, sizeof(body),
                     "{\"device_id\":\"%s\",\"event_id\":\"%s\","
                     "\"button\":\"%s\",\"source\":\"button\","
                     "\"local_feedback\":\"led\",\"press_delay_ms\":%d,"
                     "\"snapshot\":%s}",
                     DEVICE_ID, evid, bname, press_delay, snap);
    if (n <= 0 || n >= (int)sizeof(body)) {
        ESP_LOGE(TAG, "求助消息组装失败（缓冲不足）");
        return false;
    }

    bool sent = (http_post_json(PATH_HELP, body, resp, sizeof(resp)) == ESP_OK);
    if (sent) {
        ESP_LOGI(TAG, "  [1/2] 求助消息已送达 → VPS 接收成立");
    } else {
        ESP_LOGW(TAG, "  [1/2] 求助消息**没送出去**：本地灯已亮（本地确认成立），"
                      "但远端不会有任何记录 —— 这两件事本来就可以分开");
    }

    /* 3) 少量环境采样，作为这次求助的上下文。
     *    纯 best-effort：它失败不影响求助本身的成败判定 ——
     *    把"上下文没传全"当成"求助失败"，会让灯效给出错误的坏消息。 */
    sample_ctx_t c = { .request_id = evid, .trigger = "button",
                       .button = bname, .press_delay_ms = -1 };
    int ok = 0;
    for (int i = 0; i < HELP_CTX_SAMPLES; i++) {
        if (i > 0) {
            vTaskDelay(pdMS_TO_TICKS(HELP_CTX_GAP_MS));
        }
        /* 只在首条上带「按下 → 采样组装完成」的延迟 —— 它是按键响应速度的
         * 直接证据。后几条带这个数字没有意义（里面混了等待间隔和 HTTP 耗时），
         * 带了反而会被误读成"响应变慢了"。 */
        if (i == 0) {
            c.press_delay_ms = press_delay;
        } else {
            c.press_delay_ms = -1;
        }
        if (build_payload(payload, sizeof(payload), (*seq)++, &c) <= 0) {
            continue;
        }
        if (http_post_json(PATH_DATA, payload, resp, sizeof(resp)) == ESP_OK) {
            ok++;
        }
    }
    ESP_LOGI(TAG, "======== 求助 %s 结束：消息%s，上下文采样 %d/%d 条，"
                  "按下→首条 %d ms ========",
             evid, sent ? "已送达" : "未送达", ok, HELP_CTX_SAMPLES, press_delay);
    return sent;
}

/* ============ 第3周补：暂停周期上报（任务卡第2周要求） ============
 *
 * 任务卡两处都写了这件事："暂停周期上报验证按钮触发"、"暂停周期上报、保持命令通道"。
 * 它要证明的东西比页面上的数字对比硬得多：
 *
 *   停掉 1Hz 周期上报之后，数据库里**任何新增记录都只可能来自指令**。
 *   这时候点一次「采集」，新出现的那几条就是铁证 —— 不可能是页面翻出来的旧值。
 *
 * 但暂停有个明显的两难：板子只做 POST、不监听端口，指令全靠"搭车"在它自己
 * 上报请求的响应体里下发。上报一停，通道就断了。
 *
 * 解法：暂停期间把「上报传感数据」换成「心跳」——
 *   正常：POST /api/data  写一条记录 + 取指令
 *   暂停：POST /api/poll  不写任何记录，只为取指令
 * 通道不断，而库里一条新传感记录都不会有。这正是任务卡说的"保持命令通道"。
 */
static bool s_paused = false;

/* 控制指令（暂停/恢复/求助回应）的回执。一条请求只能带一个 cmd_state，
 * 所以分两次发：先 received 再 done —— 这样四阶段证据链与采集指令完全一致，
 * 网页不必为控制类指令写特殊分支。
 *
 * ⚠️ 已知限制：只有一个待发槽位。若两条控制指令在回执发完之前接连到达，
 * 前一条的回执会被后一条覆盖，前一条最终会走到 exec 超时。
 * 这是**可见**的失败（指令列表里看得到超时），不是静默丢数据；
 * 控制指令本来就很少连续下发，暂不为它引入队列。 */
static char s_ack_id[48] = "";
static int  s_ack_need = 0;

static void dispatch_command(const cmd_t *cmd, uint32_t *seq);

/* 暂停期间的心跳：不写数据，只为把命令通道留着 + 把回执送出去 */
static void poll_tick(uint32_t *seq)
{
    char body[320];
    char resp[512];

    bool have_ack = (s_ack_need > 0 && s_ack_id[0] != '\0');
    char acked[48];
    snprintf(acked, sizeof(acked), "%s", s_ack_id);   /* 本次要清掉的是哪一个 */
    int rounds = have_ack ? 2 : 1;

    for (int r = 0; r < rounds; r++) {
        char ack[128];
        ack[0] = '\0';
        if (have_ack) {
            snprintf(ack, sizeof(ack),
                     ",\"request_id\":\"%s\",\"cmd_state\":\"%s\"",
                     s_ack_id, (r == 0) ? "received" : "done");
        }
        snprintf(body, sizeof(body), "{\"device_id\":\"%s\",\"paused\":%s%s}",
                 DEVICE_ID, s_paused ? "true" : "false", ack);

        if (http_post_json(PATH_POLL, body, resp, sizeof(resp)) != ESP_OK) {
            ESP_LOGW(TAG, "[poll] 心跳失败，回执留到下次再发");
            return;                      /* 不丢回执，下次继续 */
        }
        if (have_ack) {
            ESP_LOGI(TAG, "[poll] 回执 %s → %s", acked,
                     (r == 0) ? "received" : "done");
        }
        cmd_t cmd;
        if (parse_command(resp, &cmd)) {
            dispatch_command(&cmd, seq);
        }
    }
    /* 只清掉本次真正发完的那一个：dispatch_command 可能刚设了新的回执
     * （比如暂停途中又收到 resume），不能顺手把它抹掉。 */
    if (have_ack && strcmp(s_ack_id, acked) == 0) {
        s_ack_need = 0;
        s_ack_id[0] = '\0';
    }
}

/* 指令分发：采集类走 run_command，控制类在这里就地处理 */
static void dispatch_command(const cmd_t *cmd, uint32_t *seq)
{
    ESP_LOGI(TAG, "======== 收到下行指令 %s  type=%s ========",
             cmd->request_id, cmd->type);

    if (strcmp(cmd->type, "pause") == 0) {
        s_paused = true;
        snprintf(s_ack_id, sizeof(s_ack_id), "%s", cmd->request_id);
        s_ack_need = 2;
        ESP_LOGI(TAG, "已暂停周期上报：之后只发 /api/poll 心跳，"
                      "不再产生新的传感记录（命令通道保持）");
        return;
    }
    if (strcmp(cmd->type, "resume") == 0) {
        s_paused = false;
        snprintf(s_ack_id, sizeof(s_ack_id), "%s", cmd->request_id);
        s_ack_need = 2;
        ESP_LOGI(TAG, "已恢复周期上报");
        return;
    }
    /* —— 第3周：远端对求助的回应传回设备 ——
     * 这就是任务卡说的「**迁移确认/取消机制**」：确认和取消不是网页自己的
     * 状态，而要回到发起者身边 —— 否则按了键的人永远不知道有没有人理他，
     * 三级反馈里最要紧的第三级（查看者回应）在本地就是不可见的。
     *
     * 这里**要回复执**：回执证明的不是"设备执行了什么资源操作"，而是
     * "站在设备旁边的人确实收到了'有人回应'这件事"—— 这正是闭环缺的最后一段。
     * 复用 pause/resume 那套延后回执（见 poll_tick）：
     * 一条请求只能带一个 cmd_state，所以分两次发 received → done。 */
    if (strcmp(cmd->type, "help_reply") == 0) {
        bool is_cancel = (strcmp(cmd->action, "cancel") == 0);
        ESP_LOGI(TAG, "求助 %s 的远端%s已传回 → 本地提示（%s）",
                 cmd->event_id[0] ? cmd->event_id : "(未带 id)",
                 is_cancel ? "取消" : "回应",
                 is_cancel ? "长亮 1 秒" : "慢闪 4 次");
        led_signal(is_cancel ? LED_PAT_REPLY_CANCEL : LED_PAT_REPLY_ACK);
        snprintf(s_ack_id, sizeof(s_ack_id), "%s", cmd->request_id);
        s_ack_need = 2;                 /* 延后回执：下次上报/心跳时补发 */
        return;
    }
    run_command(cmd, seq);          /* recollect：采一批新数据 */
}

/* ============ 第5周：BOOT 键录音 ============
 *
 * 分工：
 *   · 本任务只负责"开始"（立刻开始，不等主循环）和"请求结束"；
 *   · "结束 + 上传"由主循环做 —— 本项目里 HTTP 客户端只允许主循环一个使用者，
 *     两个任务同时 POST 会争用 http_client（第2周踩过）。
 */
static volatile bool s_rec_stop_req = false;

static void boot_btn_task(void *arg)
{
    gpio_config_t io = {
        .pin_bit_mask = 1ULL << BOOT_BTN_GPIO,
        .mode         = GPIO_MODE_INPUT,
        .pull_up_en   = GPIO_PULLUP_ENABLE,
        .pull_down_en = GPIO_PULLDOWN_DISABLE,
        .intr_type    = GPIO_INTR_DISABLE,
    };
    gpio_config(&io);

    int last = 1, cnt = 0;
    while (1) {
        int v = gpio_get_level(BOOT_BTN_GPIO);
        if (v != last) {
            if (++cnt >= 3) {                    /* 20ms × 3 = 60ms 消抖 */
                last = v;
                if (v == 0) {                    /* 按下 */
                    if (!mic_recording()) {
                        if (mic_start() == ESP_OK) {
                            ESP_LOGI(TAG, "======== 开始录音（再按一下 BOOT 结束）========");
                            led_signal(LED_PAT_KEY);
                        }
                    } else {
                        s_rec_stop_req = true;   /* 交给主循环收尾 */
                    }
                }
            }
        } else {
            cnt = 0;
        }
        vTaskDelay(pdMS_TO_TICKS(20));
    }
}

void app_main(void)
{
    ESP_ERROR_CHECK(nvs_flash_init());
    temp_sensor_init();
    i2c_master_init();
    i2c_scan();

    if (!qma6100p_init()) {
        ESP_LOGE(TAG, "IMU 初始化失败，停机（不发送假数据）");
        return;
    }
    imu_identify();

    /* 第3周：本地反馈 LED 与实体按键。特意放在联网之前 —— 这两个是纯本地
     * 器件，万一 WiFi 连不上，至少按键与灯效还能用来判断板子是活的。 */
    led_init();
    btn_init();
    xTaskCreate(btn_task, "btn", 3072, NULL, 5, NULL);

    /* 第5周：板载麦克风 + BOOT 录音键。
     * 麦克风初始化失败只警告 —— 第1—4周的链路不能因为一个音频器件起不来就停摆。 */
    if (mic_init() != ESP_OK) {
        ESP_LOGW(TAG, "麦克风未就绪：录音功能不可用，其余功能照常");
    }
    xTaskCreate(boot_btn_task, "bootbtn", 3072, NULL, 4, NULL);

    /* 上电自检闪灯。这一步是专门给人眼看的：灯效是本项目里唯一
     * "写代码的人不在现场就无法验证"的输出，所以开机先自证一次。
     * 看不到这 3 次闪烁 = 灯效极性反了（板子可能是高电平点亮），
     * 改 led_raw() 里的 0/1 即可，别去改别处。 */
    ESP_LOGI(TAG, "LED 自检：接下来 GPIO3 应连闪 3 次（开漏拉低点亮）");
    led_signal(LED_PAT_BOOT);

    wifi_init_sta();
    /* —— 等 WiFi 拿到 IP，但**不无限等** ——
     *
     * 原来这里是 portMAX_DELAY：拿不到 IP 就永远停在这一行，
     * 既不重试 DHCP、也不告诉任何人，整块板子看起来"死了"。
     *
     * 2026-09-23 实测踩到：AP 能关联成功（RSSI -45）、但 DHCP 不给地址，
     * 连着重启 4 次全部卡死在这里，服务器侧 82 秒一条记录都没有 ——
     * 而串口日志里唯一的线索就是"连上了、然后什么都没有"。
     *
     * 所以改成：等一会儿没拿到，就主动断开重连一次（DHCP 会在重新关联后
     * 重来一遍），并且把"我等了多久、试了几次"明确打出来。
     * 这样至少现场的人知道它在干嘛，而不是对着一块沉默的板子猜。 */
    {
        const int ip_wait_ms = 15000;
        const int dhcp_tries_before_static = 2;   /* 约 30 秒还不行就兜底 */
        int tries = 0;
        bool up = false;
        while (!up) {
            if (xEventGroupWaitBits(s_wifi_event_group, WIFI_CONNECTED_BIT,
                                    pdFALSE, pdTRUE,
                                    pdMS_TO_TICKS(ip_wait_ms)) & WIFI_CONNECTED_BIT) {
                ESP_LOGI(TAG, "WiFi 就绪（DHCP，重试 %d 次）", tries);
                up = true;
                break;
            }
            tries++;
            ESP_LOGW(TAG, "已关联但 %d 秒内没拿到 IP（第 %d 次）",
                     ip_wait_ms / 1000, tries);
            ESP_LOGW(TAG, "  如果一直这样：多半是 AP 的 DHCP 有问题"
                          "（地址池满 / 该客户端被拒），不是板子坏了 ——"
                          "可先看同一 WiFi 下别的设备能不能拿到地址。");

            if (STATIC_FALLBACK_ENABLE && tries >= dhcp_tries_before_static) {
                esp_netif_t *nif = esp_netif_get_handle_from_ifkey("WIFI_STA_DEF");
                if (nif) {
                    esp_netif_dhcpc_stop(nif);      /* 必须先停 DHCP 才能设静态地址 */
                    esp_netif_ip_info_t ip = {0};
                    ip.ip.addr      = esp_ip4addr_aton(STATIC_IP_FALLBACK);
                    ip.gw.addr      = esp_ip4addr_aton(STATIC_GW_FALLBACK);
                    ip.netmask.addr = esp_ip4addr_aton(STATIC_NETMASK_FALLBACK);
                    if (esp_netif_set_ip_info(nif, &ip) == ESP_OK) {
                        xEventGroupSetBits(s_wifi_event_group, WIFI_CONNECTED_BIT);
                        ESP_LOGW(TAG, "⚠️ DHCP 连续 %d 次失败 → 已启用**静态地址兜底** %s"
                                      "（网关 %s）",
                                 tries, STATIC_IP_FALLBACK, STATIC_GW_FALLBACK);
                        ESP_LOGW(TAG, "   这是自保措施：正常情况应当走 DHCP。"
                                      "若本网段该地址已被占用，两台设备会互相冲突，"
                                      "请改用别的地址或修好 DHCP 后重新编译。");
                        up = true;
                        break;
                    }
                    ESP_LOGE(TAG, "静态地址设置失败，继续重试 DHCP");
                }
            }
            esp_wifi_disconnect();
            vTaskDelay(pdMS_TO_TICKS(1500));
            esp_wifi_connect();
        }
    }

    /* 摄像头视频流是独立功能：就算它挂了，也不许影响 IMU 上报链路 */
    if (camera_stream_start() != ESP_OK) {
        ESP_LOGW(TAG, "摄像头未就绪，IMU 上报链路继续工作");
    }

    char payload[512];
    char resp[512];
    cmd_t cmd;
    uint32_t seq = 0;

    while (1) {
        /* —— 第5周：录音结束 → 上传 ——
         * 放在循环最前面，因为上传要独占 HTTP 客户端。
         * 录音采集本身在 I2S 任务里跑，这一秒的定时上报不会把它打断。 */
        if (s_rec_stop_req) {
            s_rec_stop_req = false;
            const uint8_t *wav = NULL;
            size_t wlen = 0;
            float  secs = 0;
            bool   trunc = false;
            if (mic_stop(&wav, &wlen, &secs, &trunc) == ESP_OK && wav && wlen > 44) {
                bool ok = http_post_binary(PATH_AUDIO, wav, wlen, resp, sizeof(resp)) == ESP_OK;
                led_signal(ok ? LED_PAT_UPLOAD_OK : LED_PAT_FAIL);
                ESP_LOGI(TAG, "录音上传%s：%.1f 秒 / %u 字节%s",
                         ok ? "成功" : "失败", secs, (unsigned)wlen,
                         trunc ? "（到 5 秒上限截断）" : "");
            }
        }

        /* —— 第3周：先响应实体按键 ——
         * 按键的语义是**发起一条教学求助测试消息**，不是"采一批数据"。
         * 非阻塞取：没有按键就继续走 1Hz 定时上报，不影响原有节奏。
         * 执行放在主循环做（而不是按键任务里），是为了让 HTTP 上报只有
         * 一个使用者：两个任务同时 POST 会争用 seq 与 http_client。
         * 代价是被按键打断的那一次定时上报会晚一点，可接受。 */
        btn_event_t ev;
        while (xQueueReceive(s_btn_q, &ev, 0) == pdTRUE) {
            /* 灯效反映的是**求助消息**的送达结果。上下文采样失败不改变结论 ——
             * 把"上下文没传全"当成"求助失败"会给出错误的坏消息，
             * 让旁边的人以为没人能收到他的求助。 */
            bool help_sent = run_help_request(&ev, &seq);
            led_signal(help_sent ? LED_PAT_UPLOAD_OK : LED_PAT_FAIL);
        }

        /* —— 第3周补：暂停期间 / 控制回执还没送完，走心跳而不是上报 ——
         * 心跳不写任何传感记录，但照样把待执行指令捎回来，所以"暂停"不会
         * 把命令通道一起停掉。这就是任务卡要的"暂停周期上报、保持命令通道"。 */
        if (s_paused || s_ack_need > 0) {
            poll_tick(&seq);
            /* 1 秒切成 20 段：录音结束时不用干等满 1 秒才处理 */
        for (int _i = 0; _i < 20 && !s_rec_stop_req; _i++) {
            vTaskDelay(pdMS_TO_TICKS(POST_INTERVAL_MS / 20));
        }
            continue;
        }

        sample_ctx_t c = { .trigger = "timer" };
        int n = build_payload(payload, sizeof(payload), seq++, &c);
        if (n > 0) {
            if (http_post_json(PATH_DATA, payload, resp, sizeof(resp)) == ESP_OK) {
                /* 第2周：响应体里可能捎带着下行指令 */
                if (parse_command(resp, &cmd)) {
                    dispatch_command(&cmd, &seq);
                }
            }
        }
        /* 1 秒切成 20 段：录音结束时不用干等满 1 秒才处理 */
        for (int _i = 0; _i < 20 && !s_rec_stop_req; _i++) {
            vTaskDelay(pdMS_TO_TICKS(POST_INTERVAL_MS / 20));
        }
    }
}
