/* 板载麦克风录音 —— 第5周
 *
 * 硬件：MSM261S4030H0R（数字 MEMS 麦克风，I2S 输出）
 *       SCK=GPIO41  WS=GPIO42  SD=GPIO2   —— 与摄像头/IMU/按键都不冲突
 *
 * 为什么用 32bit 槽位再右移 16 位：
 *   这颗麦克风输出 24 位数据、左对齐放在 32 位槽里。按 16bit 槽读会丢掉中间 8 位
 *   变成噪声；按 32bit 读再取高 16 位，等于保留 24 位里的高 16 位，对语音够用。
 *
 * 为什么缓冲放 PSRAM：
 *   5 秒 16k/16bit 单声道是 160KB，内部 RAM 只有两百多 KB 可用，
 *   放进去会把 WiFi/HTTP 的余量吃光。本板 8MB OCT PSRAM，这点占用可以忽略。
 */
#include "mic_record.h"

#include <string.h>

#include "driver/gpio.h"
#include "driver/i2s_std.h"
#include "esp_heap_caps.h"
#include "esp_log.h"
#include "freertos/FreeRTOS.h"
#include "freertos/task.h"

static const char *TAG = "mic";

#define MIC_SCK        GPIO_NUM_41
#define MIC_WS         GPIO_NUM_42
#define MIC_SD         GPIO_NUM_2
#define MIC_RATE       16000
#define MIC_MAX_SEC    5
#define MIC_MAX_SAMPLES (MIC_RATE * MIC_MAX_SEC)
#define WAV_HDR_BYTES  44

static i2s_chan_handle_t s_rx;
static TaskHandle_t      s_task;
static volatile bool     s_run;
static volatile size_t   s_len;
static volatile bool     s_truncated;
static int16_t          *s_pcm;          /* PSRAM：纯 PCM */
static uint8_t          *s_wav;          /* PSRAM：44 字节头 + PCM，上传用 */

static bool inited;

static void put32(uint8_t *p, uint32_t v)
{
    p[0] = v & 0xff;
    p[1] = (v >> 8) & 0xff;
    p[2] = (v >> 16) & 0xff;
    p[3] = (v >> 24) & 0xff;
}

static void put16(uint8_t *p, uint16_t v)
{
    p[0] = v & 0xff;
    p[1] = (v >> 8) & 0xff;
}

/* 44 字节标准 WAV 头（PCM / 单声道 / 16bit） */
static void build_wav_header(uint8_t *h, uint32_t pcm_bytes, uint32_t rate)
{
    memcpy(h, "RIFF", 4);
    put32(h + 4, 36 + pcm_bytes);
    memcpy(h + 8, "WAVE", 4);
    memcpy(h + 12, "fmt ", 4);
    put32(h + 16, 16);
    put16(h + 20, 1);              /* PCM */
    put16(h + 22, 1);              /* 单声道 */
    put32(h + 24, rate);
    put32(h + 28, rate * 2);       /* 字节率 */
    put16(h + 32, 2);              /* 块对齐 */
    put16(h + 34, 16);             /* 位深 */
    memcpy(h + 36, "data", 4);
    put32(h + 40, pcm_bytes);
}

static void mic_task(void *arg)
{
    int32_t frames[256];
    while (s_run) {
        size_t br = 0;
        esp_err_t err = i2s_channel_read(s_rx, frames, sizeof(frames), &br,
                                         pdMS_TO_TICKS(300));
        if (err != ESP_OK || br == 0) {
            continue;
        }
        size_t n = br / sizeof(int32_t);
        for (size_t i = 0; i < n; i++) {
            if (s_len >= MIC_MAX_SAMPLES) {
                s_truncated = true;
                s_run = false;
                break;
            }
            /* 24 位左对齐 → 取高 16 位 */
            int32_t v = frames[i] >> 16;
            if (v > 32767)  v = 32767;
            if (v < -32768) v = -32768;
            s_pcm[s_len++] = (int16_t)v;
        }
        if (s_len >= MIC_MAX_SAMPLES) {
            s_truncated = true;
            s_run = false;
        }
    }
    s_task = NULL;
    vTaskDelete(NULL);
}

esp_err_t mic_init(void)
{
    if (inited) {
        return ESP_OK;
    }
    if (s_pcm == NULL) {
        s_pcm = heap_caps_malloc(MIC_MAX_SAMPLES * sizeof(int16_t), MALLOC_CAP_SPIRAM);
        s_wav = heap_caps_malloc(WAV_HDR_BYTES + MIC_MAX_SAMPLES * sizeof(int16_t),
                                 MALLOC_CAP_SPIRAM);
        if (s_pcm == NULL || s_wav == NULL) {
            ESP_LOGE(TAG, "PSRAM 分配失败：录音不可用");
            return ESP_ERR_NO_MEM;
        }
    }

    i2s_chan_config_t chan_cfg = I2S_CHANNEL_DEFAULT_CONFIG(I2S_NUM_0, I2S_ROLE_MASTER);
    chan_cfg.auto_clear = true;
    esp_err_t err = i2s_new_channel(&chan_cfg, NULL, &s_rx);
    if (err != ESP_OK) {
        ESP_LOGE(TAG, "i2s_new_channel: %s", esp_err_to_name(err));
        return err;
    }

    i2s_std_config_t std_cfg = {
        .clk_cfg  = I2S_STD_CLK_DEFAULT_CONFIG(MIC_RATE),
        /* 32bit 槽位：麦克风的 24 位数据是左对齐的（见文件头说明） */
        .slot_cfg = I2S_STD_PHILIPS_SLOT_DEFAULT_CONFIG(I2S_DATA_BIT_WIDTH_32BIT,
                                                       I2S_SLOT_MODE_MONO),
        .gpio_cfg = {
            .mclk = I2S_GPIO_UNUSED,
            .bclk = MIC_SCK,
            .ws   = MIC_WS,
            .dout = I2S_GPIO_UNUSED,
            .din  = MIC_SD,
            .invert_flags = { false, false, false },
        },
    };
    /* 单声道取左槽：MSM261 的 L/R 在板上接地，数据在左通道 */
    std_cfg.slot_cfg.slot_mask = I2S_STD_SLOT_LEFT;

    err = i2s_channel_init_std_mode(s_rx, &std_cfg);
    if (err != ESP_OK) {
        ESP_LOGE(TAG, "i2s_channel_init_std_mode: %s", esp_err_to_name(err));
        return err;
    }
    err = i2s_channel_enable(s_rx);
    if (err != ESP_OK) {
        ESP_LOGE(TAG, "i2s_channel_enable: %s", esp_err_to_name(err));
        return err;
    }
    inited = true;
    ESP_LOGI(TAG, "麦克风就绪：SCK=%d WS=%d SD=%d  %d Hz / 单声道 / %d 秒上限",
             MIC_SCK, MIC_WS, MIC_SD, MIC_RATE, MIC_MAX_SEC);
    return ESP_OK;
}

bool mic_recording(void)
{
    return s_run;
}

esp_err_t mic_start(void)
{
    if (!inited) {
        esp_err_t err = mic_init();
        if (err != ESP_OK) {
            return err;
        }
    }
    if (s_run) {
        return ESP_OK;          /* 已经在录了 */
    }
    s_len = 0;
    s_truncated = false;
    s_run = true;
    if (xTaskCreate(mic_task, "mic_rec", 4096, NULL, 6, &s_task) != pdPASS) {
        s_run = false;
        ESP_LOGE(TAG, "录音任务创建失败");
        return ESP_FAIL;
    }
    return ESP_OK;
}

esp_err_t mic_stop(const uint8_t **wav, size_t *len, float *seconds, bool *truncated)
{
    if (wav == NULL || len == NULL) {
        return ESP_ERR_INVALID_ARG;
    }
    s_run = false;
    /* 等采集任务自己退出（它每轮最多阻塞 300ms） */
    for (int i = 0; i < 40 && s_task != NULL; i++) {
        vTaskDelay(pdMS_TO_TICKS(20));
    }

    uint32_t pcm_bytes = (uint32_t)(s_len * sizeof(int16_t));
    build_wav_header(s_wav, pcm_bytes, MIC_RATE);
    if (pcm_bytes) {
        memcpy(s_wav + WAV_HDR_BYTES, s_pcm, pcm_bytes);
    }
    *wav = s_wav;
    *len = WAV_HDR_BYTES + pcm_bytes;
    if (seconds) {
        *seconds = (float)s_len / (float)MIC_RATE;
    }
    if (truncated) {
        *truncated = s_truncated;
    }
    ESP_LOGI(TAG, "录音结束：%.1f 秒 / %u 字节%s", (float)s_len / MIC_RATE,
             (unsigned)*len, s_truncated ? "（到上限截断）" : "");
    return ESP_OK;
}
