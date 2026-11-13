"""Host-driver shutdown state and the fixed MCU touch transaction."""
import importlib.util
from pathlib import Path
import sys
import threading
from types import ModuleType, SimpleNamespace
import unittest
from unittest.mock import patch

ROOT=Path(__file__).resolve().parents[1]

def load(name):
    package=ModuleType('test_drivers'); package.__path__=[]
    bus=ModuleType('test_drivers.bus')
    with patch.dict(sys.modules, {'mcu':ModuleType('mcu'),'test_drivers':package,'test_drivers.bus':bus}):
        spec=importlib.util.spec_from_file_location('test_drivers.'+name,ROOT/'klipper'/(name+'.py'))
        module=importlib.util.module_from_spec(spec);spec.loader.exec_module(module)
    return module

class DriverTests(unittest.TestCase):
    def test_only_initialized_opted_in_display_survives_shutdown(self):
        driver=load('fsmc_display')
        for enabled,initialized in ((True,True),(False,True),(True,False)):
            obj=driver.FSMCDisplay.__new__(driver.FSMCDisplay)
            obj.shutdown_ui=enabled;obj.initialized=initialized;obj.is_ready=True;obj.init_epoch=0
            obj.handle_shutdown()
            self.assertEqual(obj.is_ready,enabled and initialized)
            self.assertTrue(obj.in_shutdown)
            self.assertEqual(obj.init_epoch,1)
            obj.handle_not_ready()
            self.assertFalse(obj.is_ready)

    def test_shutdown_ping_explicitly_selects_ui_and_updates_epoch(self):
        driver=load('fsmc_display')
        obj=driver.FSMCDisplay.__new__(driver.FSMCDisplay)
        obj.shutdown_ui=True;obj.in_shutdown=True;obj.oid=7
        obj.ui_epoch=0;obj.init_epoch=0;obj.sync_mutex=threading.Lock()
        calls=[]
        obj.ui_ping=SimpleNamespace(send=lambda args,**kw:(calls.append(args) or {'epoch':3,'shutdown':1}))
        obj.wait_idle();obj.wait_idle()
        self.assertEqual(calls,[[7,1],[7,1]])
        self.assertEqual(obj.init_epoch,1)

    def test_touch_shutdown_never_uses_arbitrary_spi_transfer(self):
        driver=load('fsmc_touch')
        obj=driver.FSMCTouch.__new__(driver.FSMCTouch)
        obj.mutex=threading.Lock();obj.touch_oid=8;obj.request=[0]*27;obj.min_pressure=100
        values=[150,4000,2100,2101,2099,1800,1801,1799,0]
        data=b''.join(bytes([0,(v<<3)>>8,(v<<3)&255]) for v in values)
        calls=[]
        obj.touch_read=SimpleNamespace(send=lambda args,**kw:(calls.append(args) or {'response':data}))
        obj.spi=SimpleNamespace(spi_transfer=lambda *a,**kw:self.fail('Generic SPI must not be used'))
        obj.is_ready=True;obj.shutdown_ui=True;obj._shutdown()
        result=obj.read()
        self.assertEqual(calls,[[8]])
        self.assertEqual((result['x'],result['y']),(2100,1800))
        self.assertTrue(result['pressed'])
        obj.shutdown_ui=False;obj._shutdown()
        self.assertFalse(obj.is_ready)

if __name__=='__main__':unittest.main()
