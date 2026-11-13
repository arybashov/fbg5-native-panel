#!/usr/bin/env python3
"""Build only; never flash. Pass an isolated checkout of the pinned Klipper base."""
import argparse
import json
from pathlib import Path
import shutil
import subprocess
import sys

BASE = '29d4ec2d6d840ee839333306003a400da186d41d'

def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('checkout', type=Path)
    args = parser.parse_args()
    tree = args.checkout.resolve()
    source = Path(__file__).resolve().parent
    sha = subprocess.check_output(['git','-C',str(tree),'rev-parse','HEAD'],text=True).strip()
    if sha != BASE:
        raise SystemExit('Expected pinned Klipper base '+BASE)
    if 'config WANT_FSMC_LCD' not in (tree/'src/Kconfig').read_text():
        subprocess.run(['git','apply',str(source/'build-integration.patch')],cwd=tree,check=True)
    for name in ('fsmc_lcd.c','fsmc_touch.c','fsmc_fallback_font.h'):
        shutil.copyfile(source/name,tree/'src/stm32'/name)
    for name in ('fsmc_display.py','fsmc_touch.py'):
        shutil.copyfile(source.parent/'klipper'/name,tree/'klippy/extras'/name)
    shutil.copyfile(source/'robin-nano-v12.config',tree/'.config')
    subprocess.run(['make','olddefconfig'],cwd=tree,check=True)
    subprocess.run(['make','-j2'],cwd=tree,check=True)
    subprocess.run([sys.executable,str(tree/'scripts/update_mks_robin.py'),
                    str(tree/'out/klipper.bin'),str(tree/'out/Robin_nano35.bin')],check=True)
    dictionary=json.loads((tree/'out/klipper.dict').read_text())
    assert dictionary['config']['FSMC_SHUTDOWN_UI']==1
    print('Built:',dictionary['version'],(tree/'out/klipper.bin').stat().st_size,'bytes')

if __name__=='__main__':
    main()
