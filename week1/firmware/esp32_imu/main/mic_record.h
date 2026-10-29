#pragma once
/* 板载麦克风录音（第5周）。 */
#include <stdbool.h>
#include <stddef.h>
#include "esp_err.h"

/* 初始化 I2S 接收（SCK=41 / WS=42 / SD=2，MSM261S4030H0R）。重复调用无副作用。 */
esp_err_t mic_init(void);

/* 开始录音：启动采集任务，数据攒在 PSRAM 里。 */
esp_err_t mic_start(void);

/* 停止录音并封好 WAV 头。
 * 返回的指针在**下一次 mic_start 之前**有效，调用方不要 free。 */
esp_err_t mic_stop(const uint8_t **wav, size_t *len, float *seconds, bool *truncated);

bool mic_recording(void);

/* 是否因为录满上限而自动停了（取一次就清零）。
 * 为什么需要它：录满上限时采集任务自己会停，但主逻辑不知道 ——
 * 表现就是"再按一下 BOOT 会重新开一段，上一段直接丢"。
 * 有这个标志，主逻辑才能在录满的那一刻补一次"结束并上传"。 */
bool mic_take_auto_stop(void);
