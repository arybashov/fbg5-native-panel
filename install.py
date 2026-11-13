#!/usr/bin/env python3
"""Install/restore the host panel for an existing /opt/fbg5 Klipper setup.

Never flashes MCU firmware. Requires already-working fsmc_display firmware.
"""
import argparse
import datetime
import hashlib
import json
import os
from pathlib import Path
import pwd
import shutil
import subprocess
import time
import urllib.request


def api(endpoint, post=False):
    req=urllib.request.Request('http://127.0.0.1:7125/'+endpoint,
                              data=b'' if post else None,method='POST' if post else 'GET')
    return json.load(urllib.request.urlopen(req,timeout=12))['result']


def require_safe():
    s=api('printer/objects/query?webhooks&print_stats&extruder&heater_bed&virtual_sdcard')['status']
    if s['webhooks']['state']!='ready' or s['print_stats']['state'] not in ('standby','complete','cancelled') or s['virtual_sdcard']['is_active']:
        raise RuntimeError('Installation/rollback requires a ready, idle printer')
    if any(s[h]['target'] or s[h]['power'] for h in ('extruder','heater_bed')):
        raise RuntimeError('Turn heaters off before installation/rollback')


def main():
    parser=argparse.ArgumentParser(description=__doc__)
    choice=parser.add_mutually_exclusive_group(required=True)
    choice.add_argument('--install',action='store_true')
    choice.add_argument('--rollback',type=Path,help='Manifest printed by a previous install')
    parser.add_argument('--base',type=Path,default=Path('/opt/fbg5'))
    parser.add_argument('--user',default='fbg5')
    args=parser.parse_args()
    if os.geteuid()!=0:raise SystemExit('Run as root')
    base=args.base.resolve()
    if not base.is_dir() or base==Path('/') or any(c.isspace() for c in str(base)):
        raise SystemExit('Existing base directory without spaces is required')
    account=pwd.getpwnam(args.user)
    require_safe()
    service=Path('/etc/systemd/system/fbg5-screen.service')
    cfg=base/'printer_data/config/printer.cfg'
    if args.rollback:
        manifest=json.loads(args.rollback.read_text())
        if manifest['base']!=str(base):raise SystemExit('Backup belongs to a different installation')
        files=manifest['files']
        for dest,record in files.items():
            path=Path(dest).resolve()
            if path!=service and not path.is_relative_to(base):
                raise SystemExit('Unsafe destination in manifest')
            if record and hashlib.sha256(Path(record['backup']).read_bytes()).hexdigest()!=record['sha256']:
                raise SystemExit('Backup checksum mismatch')
        subprocess.run(['systemctl','stop','fbg5-screen'],check=True)
        for dest,record in files.items():
            path=Path(dest)
            if record:
                shutil.copy2(record['backup'],path)
            else:
                path.unlink(missing_ok=True)
        subprocess.run(['systemctl','daemon-reload'],check=True)
        api('printer/firmware_restart',True)
        subprocess.run(['systemctl','start','fbg5-screen'],check=True)
        print('Previous panel restored')
        return
    source=Path(__file__).resolve().parent
    constants=api('printer/objects/query?mcu')['status']['mcu'].get('mcu_constants',{})
    if constants.get('FSMC_SHUTDOWN_UI')!=1:
        raise SystemExit('Install the matching shutdown-UI MCU firmware before these host drivers')
    if not (base/'screen-env/bin/python').exists():raise SystemExit('Create screen-env and install requirements first')
    if not (base/'klipper/klippy/extras/fsmc_display.py').exists():raise SystemExit('Install/test FSMC display first')
    cfg_text=cfg.read_text()
    if '[include fbg5-display.cfg]' not in cfg_text:raise SystemExit('Expected working fbg5-display.cfg include')
    stamp=datetime.datetime.now(datetime.timezone.utc).strftime('%Y%m%dT%H%M%SZ')
    backup=base/'backups'/('native-panel-'+stamp)
    backup.mkdir(parents=True,exist_ok=False)
    destination=base/'native-panel'
    targets=[cfg,service,destination/'panel.py',base/'klipper/klippy/extras/fsmc_touch.py',base/'printer_data/config/fbg5-panel.cfg',destination/'settings.json',base/'klipper/klippy/extras/fsmc_display.py',base/'printer_data/config/fbg5-display.cfg']
    files={}
    for i,path in enumerate(targets):
        if path.exists():
            saved=backup/('%d-%s'%(i,path.name))
            shutil.copy2(path,saved)
            files[str(path)]={'backup':str(saved),'sha256':hashlib.sha256(saved.read_bytes()).hexdigest()}
        else:files[str(path)]=None
    manifest={'base':str(base),'files':files,'installed_utc':stamp}
    manifest_path=backup/'manifest.json'
    manifest_path.write_text(json.dumps(manifest,indent=2)+'\n')
    print('Rollback manifest:',manifest_path,flush=True)
    # Finish all preflight checks before replacing the working service.
    subprocess.run(['systemctl','stop','fbg5-screen'],check=True)
    destination.mkdir(exist_ok=True)
    os.chown(destination,account.pw_uid,account.pw_gid)
    for src,dst in [(source/'panel.py',destination/'panel.py'),
                    (source/'klipper/fsmc_touch.py',targets[3]),
                    (source/'config/fbg5-panel.cfg',targets[4]),
                    (source/'klipper/fsmc_display.py',targets[6])]:
        shutil.copyfile(src,dst)
        dst.chmod(0o644)
        os.chown(dst,account.pw_uid,account.pw_gid)
    display_cfg=targets[7]
    text=display_cfg.read_text()
    import re
    text=re.sub(r'^shutdown_ui:.*\n?', '', text, flags=re.M)
    display_cfg.write_text(text.rstrip()+'\nshutdown_ui: True\n')
    cfg_text=cfg_text.replace('[include fbg5-touch.cfg]\n','')
    if '[include fbg5-panel.cfg]' not in cfg_text:
        cfg_text='[include fbg5-panel.cfg]\n'+cfg_text
    cfg.write_text(cfg_text)
    settings=destination/'settings.json'
    if not settings.exists():
        settings.write_text(json.dumps({'socket':str(base/'printer_data/comms/klippy.sock'),
            'calibration':str(base/'printer_data/config/panel-calibration.json'),
            'state_file':str(base/'printer_data/logs/panel-state.json')},indent=2)+'\n')
    os.chown(settings,account.pw_uid,account.pw_gid)
    service.write_text('''[Unit]
Description=Flying Bear Ghost 5 native touchscreen controls
Requires=klipper.service
After=klipper.service
PartOf=klipper.service
ConditionPathExists=%s/printer_data/config/fbg5-panel.cfg

[Service]
Type=simple
User=%s
Group=%s
WorkingDirectory=%s
ExecStart=%s/screen-env/bin/python %s/native-panel/panel.py --settings %s/native-panel/settings.json
Restart=on-failure
RestartSec=3
NoNewPrivileges=true

[Install]
WantedBy=klipper.service
'''%(base,args.user,args.user,base,base,base,base))
    subprocess.run(['systemd-analyze','verify',str(service)],check=True)
    subprocess.run(['systemctl','daemon-reload'],check=True)
    subprocess.run(['systemctl','enable','fbg5-screen'],check=True)
    api('printer/firmware_restart',True)
    for _ in range(30):
        time.sleep(1)
        try:
            if api('printer/info')['state']=='ready':break
        except Exception:pass
    else:raise RuntimeError('Klipper did not become ready; use backup manifest for recovery')
    subprocess.run(['systemctl','start','fbg5-screen'],check=True)
    print('Installed. Touch calibration is required on first start.')


if __name__=='__main__':main()
