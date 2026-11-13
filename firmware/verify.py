"""Verify compiled command permissions and unchanged Klipper safety sources."""
from pathlib import Path
import re
import subprocess
import sys

tree=Path(sys.argv[1]).resolve()
allowed={'command_identify','command_clear_shutdown','command_emergency_stop',
         'command_get_uptime','command_get_clock','command_get_config',
         'command_debug_nop','command_debug_ping','command_debug_write',
         'command_debug_read','command_reset',
         'command_fsmc_lcd_cmd','command_fsmc_lcd_fill','command_fsmc_lcd_pixels',
         'command_fsmc_lcd_rle','command_fsmc_lcd_ping','command_fsmc_touch_read'}
generated=(tree/'out/compile_time_request.c').read_text()
flags={name:int(flag,0) for flag,name in re.findall(
    r'\.flags=(0x[0-9a-f]+|\d+),\s*\.func=(\w+)',generated)}
assert {name for name,flag in flags.items() if flag & 1}==allowed
for name in ('sched.c','command.c','stepper.c','pwmcmds.c','gpiocmds.c',
             'adccmds.c','spicmds.c','stm32/serial.c','stm32/watchdog.c'):
    original=subprocess.check_output(['git','show','HEAD:src/'+name],cwd=tree)
    assert (tree/'src'/name).read_bytes()==original,name
print('Compiled shutdown allowlist verified; heater/motion/scheduler/SPI/serial/watchdog sources unchanged')
