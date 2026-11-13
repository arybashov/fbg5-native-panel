import base64
import copy
import configparser
import importlib.util
import json
import os
from pathlib import Path
import struct
import tempfile
import threading
import time
import unittest
from types import SimpleNamespace
from unittest.mock import patch

import panel


def status(state='standby', hot=False):
    return {'webhooks':{'state':'ready'}, 'print_stats':{'state':state},
            'virtual_sdcard':{'is_active':state=='printing','progress':.36},
            'pause_resume':{'is_paused':state=='paused'},
            'extruder':{'can_extrude':hot,'temperature':200 if hot else 25,'target':0,'power':0},
            'heater_bed':{'temperature':25,'target':0,'power':0}}


class FakeRPC:
    def __init__(self, state):
        self.status=state
        self.calls=[]
        self.on_script=None
        self.ignore_heat=False

    def call(self, method, params=None):
        self.calls.append((method,params))
        if method=='objects/query':
            return {'status':copy.deepcopy(self.status)}
        if method=='gcode/help':
            return {name:'' for name in ('FBG_PANEL_HEAT','FBG_PANEL_EXTRUDE','FBG_PANEL_RESTART')}
        if method=='gcode/script' and params['script'].startswith('FBG_PANEL_HEAT ') and not self.ignore_heat:
            self.status['extruder']['target']=float(params['script'].split('TEMP=')[1])
        if self.on_script:
            self.on_script(params['script'])
        return {}


class PanelTests(unittest.TestCase):
    def setUp(self):
        self.tmp=tempfile.TemporaryDirectory()
        self.app=panel.Panel({'calibration':self.tmp.name+'/cal.json', 'state_file':self.tmp.name+'/state.json'})

    def tearDown(self):
        self.tmp.cleanup()

    def test_print_pause_and_shutdown_reject_controls(self):
        for state in ('printing','paused','error'):
            for action in ('heat','cool','load','restart-host'):
                rpc=FakeRPC(status(state,True))
                with self.assertRaises(ValueError):
                    self.app.execute((action,200,5,2),rpc)
                self.assertFalse(any(m=='gcode/script' for m,p in rpc.calls))
        s=status();s['webhooks']['state']='shutdown'
        with self.assertRaises(ValueError):panel.require_idle(s)

    def test_cold_nozzle_cannot_extrude(self):
        rpc=FakeRPC(status())
        for action in ('load','unload','purge','extrude','retract'):
            with self.assertRaises(ValueError):self.app.execute((action,200,5,2),rpc)
        self.assertFalse(any(m=='gcode/script' for m,p in rpc.calls))

    def test_cancel_limits_queue_to_one_millimeter(self):
        rpc=FakeRPC(status(hot=True))
        rpc.on_script=lambda script:self.app.cancel.set()
        result=self.app.execute(('load',200,5,2),rpc)
        scripts=[p['script'] for m,p in rpc.calls if m=='gcode/script']
        self.assertEqual(scripts,['FBG_PANEL_EXTRUDE DIST=1.000 SPEED=2.000'])
        self.assertIn('отменена',result)

    def test_state_change_stops_following_chunk(self):
        rpc=FakeRPC(status(hot=True))
        rpc.on_script=lambda script:rpc.status.update(status('printing',True))
        with self.assertRaises(ValueError):self.app.execute(('unload',200,5,2),rpc)
        self.assertEqual(len([m for m,p in rpc.calls if m=='gcode/script']),1)

    def test_manual_motion_is_bounded_and_negative_for_retract(self):
        rpc=FakeRPC(status(hot=True))
        self.app.execute(('retract',200,5,2),rpc)
        scripts=[p['script'] for m,p in rpc.calls if m=='gcode/script']
        self.assertEqual(scripts,['FBG_PANEL_EXTRUDE DIST=-1.000 SPEED=2.000']*5)

    def test_restart_rejects_heater_target(self):
        s=status();s['heater_bed']['target']=60
        rpc=FakeRPC(s)
        with self.assertRaises(ValueError):self.app.execute(('restart-mcu',200,5,2),rpc)
        self.assertFalse(any(m=='gcode/script' for m,p in rpc.calls))

    def test_emergency_uses_direct_api_despite_busy_worker(self):
        seen=[];event=threading.Event()
        class EmergencyRPC:
            def __init__(self,*a,**kw):pass
            def call(self,method,params=None):seen.append(method);event.set()
            def close(self):pass
        self.app.busy=True
        with patch.object(panel,'RPC',EmergencyRPC):
            self.app.emergency()
            self.assertTrue(event.wait(1))
        self.assertEqual(seen,['emergency_stop'])
        self.assertTrue(self.app.cancel.is_set())

    def test_calibration_rotation_and_inversion(self):
        targets=self.app.TARGETS
        raw=[(3900-y*11,200+x*7) for x,y in targets]
        transform,errors=panel.fit_affine(raw,targets)
        self.assertLess(max(errors),.001)
        self.assertLess(math_dist(panel.map_touch(transform,3900-200*11,200+300*7),(300,200)),.001)

    def test_bad_calibration_is_rejected(self):
        with self.assertRaises(ValueError):panel.fit_affine([(200,200)]*5,self.app.TARGETS)
        raw=[(3900-y*11,200+x*7) for x,y in self.app.TARGETS]
        raw[-1]=(3500,3500)
        with self.assertRaises(ValueError):panel.fit_affine(raw,self.app.TARGETS)

    def test_render_keeps_stop_and_readings_on_all_pages(self):
        self.app.transform=[[1,0,0],[0,1,0]]
        self.app.status=status(hot=True);self.app.status_time=time.monotonic()
        for page in ('home','more','manual','temperature'):
            self.app.page=page
            image,buttons=self.app.renderer.render(self.app.model())
            self.assertEqual(image.size,(480,320))
            self.assertIn(('stop',panel.STOP),buttons)
            self.assertNotEqual(image.getpixel((10,40)),image.getpixel((0,0)))
            for action,r in buttons:
                self.assertTrue(0<=r[0]<r[2]<480 and 0<=r[1]<r[3]<320,(action,r))
            for i,(name,a) in enumerate(buttons):
                for other,b in buttons[i+1:]:
                    self.assertFalse(a[0]<b[2] and b[0]<a[2] and a[1]<b[3] and b[1]<a[3],(name,other))

    def test_calibration_has_no_command_buttons(self):
        self.app.status=status();self.app.status_time=time.monotonic()
        _,buttons=self.app.renderer.render(self.app.model())
        self.assertEqual(buttons,[])

    def test_progress_has_estimated_eta_only_when_meaningful(self):
        s=status('printing');s['print_stats']['print_duration']=2800
        p,remain=panel.progress_info(s)
        self.assertAlmostEqual(p,.36)
        self.assertAlmostEqual(remain,4977.7777777,places=5)
        s['virtual_sdcard']['progress']=.001
        self.assertIsNone(panel.progress_info(s)[1])

    def test_heat_and_restart_queue_immediately_without_confirmation(self):
        for action in ('heat','restart-host','restart-mcu'):
            self.app.busy=False
            self.app.action(action)
            self.assertEqual(self.app.commands.get_nowait()[0],action)
            self.assertTrue(self.app.busy)

    def test_silent_ignored_heating_is_not_reported_as_success(self):
        rpc=FakeRPC(status())
        rpc.ignore_heat=True
        with self.assertRaisesRegex(RuntimeError,'не установил'):
            self.app.execute(('heat',200,5,2),rpc)

    def test_successful_heat_is_verified_by_actual_target(self):
        rpc=FakeRPC(status())
        self.assertEqual(self.app.execute(('heat',200,5,2),rpc),'Нагрев до 200 °C')
        self.assertEqual(rpc.status['extruder']['target'],200)
        self.app.execute(('cool',200,5,2),rpc)
        self.assertEqual(rpc.status['extruder']['target'],0)

    def test_cool_waits_for_zero_power_after_target_is_cleared(self):
        rpc=FakeRPC(status(hot=True))
        rpc.status['extruder'].update(target=200,power=1)
        with patch.object(panel.time,'sleep',side_effect=lambda _:rpc.status['extruder'].update(power=0)) as wait:
            self.assertEqual(self.app.execute(('cool',200,5,2),rpc),'Нагрев сопла выключен')
        wait.assert_called_once_with(.1)

    def test_cool_does_not_accept_power_on_or_missing_as_disabled(self):
        for power in (1,None):
            rpc=FakeRPC(status(hot=True))
            rpc.status['extruder']['power']=power
            with patch.object(panel.time,'monotonic',side_effect=[0,3]):
                with self.assertRaisesRegex(RuntimeError,'Выключение не подтверждено'):
                    self.app.execute(('cool',200,5,2),rpc)

    def test_cool_does_not_accept_zeroes_from_error_state(self):
        rpc=FakeRPC(status())
        rpc.on_script=lambda _:rpc.status['webhooks'].update(state='error')
        with self.assertRaisesRegex(RuntimeError,'результат команды не подтверждён'):
            self.app.execute(('cool',200,5,2),rpc)

    def test_heater_state_distinguishes_hot_but_off_from_active_target(self):
        self.assertEqual(panel.nozzle_state({'target':0,'power':0,'temperature':82}),
                         ('Нагрев выключен','Сопло горячее • мощность 0%'))
        self.assertEqual(panel.nozzle_state({'target':0,'power':1,'temperature':82})[0],
                         'Отключение нагрева…')
        self.assertEqual(panel.nozzle_state({'target':200,'power':0,'temperature':200})[0],
                         'Температура достигнута')
        self.assertEqual(panel.nozzle_state({'target':0,'power':0,'temperature':30})[1],'Мощность 0%')
        self.assertEqual(panel.nozzle_state({'target':0})[0],'Нет данных о нагреве')

    def test_home_has_four_actions_plus_stop(self):
        self.app.transform=[[1,0,0],[0,1,0]]
        self.app.status=status(hot=True);self.app.status_time=time.monotonic()
        _,buttons=self.app.renderer.render(self.app.model())
        self.assertEqual({action for action,r in buttons},{'stop','load','unload','heat','more-page'})

    def test_macro_names_are_extended_commands_without_embedded_digits(self):
        cfg=configparser.ConfigParser(interpolation=None)
        cfg.read(Path(__file__).resolve().parents[1]/'config/fbg5-panel.cfg')
        for section in cfg.sections():
            if section.startswith('gcode_macro '):
                self.assertRegex(section.split()[1],r'^[A-Z_]+$')

    def test_delta_encoding_restores_exact_frame(self):
        old=bytes([0,0])*480*320
        new=bytearray(old)
        for x,y,color in [(1,1,0xffff),(479,319,0x1234),(230,128,0xabcd)]:
            struct.pack_into('<H',new,(y*480+x)*2,color)
        rebuilt=bytearray(old)
        for x0,y0,x1,y1,encoded in panel.rectangles(old,bytes(new)):
            stream=base64.b64decode(encoded);pixels=bytearray()
            for i in range(0,len(stream),3):pixels.extend(stream[i+1:i+3]*((stream[i]&127)+1))
            stride=(x1-x0+1)*2
            self.assertEqual(len(pixels),stride*(y1-y0+1))
            for row in range(y0,y1+1):
                rebuilt[(row*480+x0)*2:(row*480+x1+1)*2]=pixels[(row-y0)*stride:(row-y0+1)*stride]
        self.assertEqual(rebuilt,new)


def math_dist(a,b):
    return sum((x-y)**2 for x,y in zip(a,b))**.5


@unittest.skipUnless(os.environ.get('KLIPPER_GCODE_PATH'),'Set KLIPPER_GCODE_PATH to run actual Klipper parser regression')
class KlipperParserTests(unittest.TestCase):
    def test_every_panel_macro_dispatches_with_parameters(self):
        spec=importlib.util.spec_from_file_location('klipper_gcode',os.environ['KLIPPER_GCODE_PATH'])
        mod=importlib.util.module_from_spec(spec);spec.loader.exec_module(mod)
        printer=SimpleNamespace(get_start_args=lambda:{},register_event_handler=lambda *a:None,
            get_reactor=lambda:SimpleNamespace(mutex=threading.RLock),config_error=ValueError,
            send_event=lambda *a:None)
        gcode=mod.GCodeDispatch(printer)
        seen=[]
        cfg=configparser.ConfigParser(interpolation=None)
        cfg.read(Path(__file__).resolve().parents[1]/'config/fbg5-panel.cfg')
        for section in cfg.sections():
            if section.startswith('gcode_macro '):
                name=section.split()[1]
                gcode.register_command(name,lambda cmd:seen.append((cmd.get_command(),cmd.get_command_parameters())))
        gcode.is_printer_ready=True
        gcode.gcode_handlers=gcode.ready_gcode_handlers
        for name in ('FBG_PANEL_HEAT','FBG_PANEL_EXTRUDE','FBG_PANEL_RESTART'):
            gcode.run_script(name+' TEMP=200 DIST=1 SPEED=2 MCU=1')
            self.assertEqual(seen[-1],(name,{'TEMP':'200','DIST':'1','SPEED':'2','MCU':'1'}))


if __name__=='__main__':unittest.main()
