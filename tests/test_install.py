"""Installer must reload Python drivers, not only recreate Printer objects."""
import importlib.util
from pathlib import Path
from types import SimpleNamespace
import unittest
from unittest.mock import patch

spec=importlib.util.spec_from_file_location('panel_install',Path(__file__).resolve().parents[1]/'install.py')
installer=importlib.util.module_from_spec(spec)
spec.loader.exec_module(installer)


class InstallerTests(unittest.TestCase):
    def test_reload_process_and_ignore_old_moonraker_status(self):
        events=[]
        responses=iter([
            {'process_id':41,'state':'ready'},  # Old host still cached by Moonraker.
            {'process_id':42,'state':'startup'},
            {'process_id':42,'state':'error'},  # Old MCU config may need reset.
            {'process_id':42,'state':'startup'},
            {'process_id':42,'state':'ready'},
        ])
        def run(args,**kwargs):
            events.append(tuple(args))
            return SimpleNamespace(stdout='42\n')
        def api(endpoint,post=False):
            if post:
                events.append(endpoint)
                return 'ok'
            info=next(responses)
            events.append((info['process_id'],info['state']))
            return info
        with patch.object(installer.subprocess,'run',side_effect=run), \
             patch.object(installer,'api',side_effect=api), \
             patch.object(installer.time,'sleep'):
            installer.restart_host()
        self.assertEqual(events[0],('systemctl','restart','klipper'))
        reset=events.index('printer/firmware_restart')
        self.assertGreater(reset,events.index((42,'error')))
        self.assertEqual(events[-2:],[(42,'ready'),('systemctl','start','fbg5-screen')])

    def test_connection_timeout_keeps_actual_error_in_message(self):
        with patch.object(installer.time,'monotonic',side_effect=[0,0,50]), \
             patch.object(installer.time,'sleep'), \
             patch.object(installer,'api',return_value={
                 'state':'error','state_message':"Option 'shutdown_ui' is not valid"}):
            with self.assertRaisesRegex(RuntimeError,"shutdown_ui"):
                installer.wait_for_host(lambda info:info['state']=='ready','Not ready')

    def test_failed_host_restart_does_not_send_mcu_commands(self):
        with patch.object(installer.subprocess,'run',side_effect=RuntimeError('restart failed')), \
             patch.object(installer,'api') as api:
            with self.assertRaisesRegex(RuntimeError,'restart failed'):
                installer.restart_host()
            api.assert_not_called()


if __name__=='__main__':unittest.main()
