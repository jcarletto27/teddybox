#pragma once
#include <stdbool.h>

void battery_init(void);
/* Called once per second. True requests orderly shutdown. */
bool battery_poll(void);
