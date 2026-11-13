// Fixed read-only XPT2046 transaction, also available during shutdown.
// GPL-3.0-or-later. Generic spi_send/spi_transfer remain blocked in shutdown.
#include "basecmd.h"
#include "command.h"
#include "spicmds.h"

struct fsmc_touch { struct spidev_s *spi; };

void command_config_fsmc_touch(uint32_t *args)
{
    struct fsmc_touch *t = oid_alloc(args[0], command_config_fsmc_touch,
                                   sizeof(*t));
    t->spi = spidev_oid_lookup(args[1]);
}
DECL_COMMAND(command_config_fsmc_touch, "config_fsmc_touch oid=%c spi_oid=%c");

void command_fsmc_touch_read(uint32_t *args)
{
    struct fsmc_touch *t = oid_lookup(args[0], command_config_fsmc_touch);
    uint8_t data[] = {0xb0,0,0, 0xc0,0,0, 0xd0,0,0, 0xd0,0,0,
                      0xd0,0,0, 0x90,0,0, 0x90,0,0, 0x90,0,0, 0x80,0,0};
    spidev_transfer(t->spi, 1, sizeof(data), data);
    sendf("fsmc_touch_response oid=%c response=%*s", args[0], sizeof(data), data);
}
DECL_COMMAND_FLAGS(command_fsmc_touch_read, HF_IN_SHUTDOWN,
                   "fsmc_touch_read oid=%c");
