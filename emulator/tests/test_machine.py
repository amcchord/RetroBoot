import copy
import hashlib
import importlib.util
import json
from pathlib import Path
import tempfile
import unittest

script=Path(__file__).resolve().parents[1]/'scripts/machine.py'
spec=importlib.util.spec_from_file_location('machine',script);machine=importlib.util.module_from_spec(spec);spec.loader.exec_module(machine)

class MachineContract(unittest.TestCase):
    def setUp(self):
        self.tmp=tempfile.TemporaryDirectory();self.addCleanup(self.tmp.cleanup);root=Path(self.tmp.name)
        package=root/'package';run=root/'run';share=root/'share'
        run.mkdir();share.mkdir();files={}
        for n in ['bin/qemu-system-ppc','share/qemu/openbios-ppc','share/qemu/qemu_vga.ndrv','share/qemu/ndrvloader']:
            f=package/n;f.parent.mkdir(parents=True,exist_ok=True);f.write_bytes(b'fixture');files[n]={'sha256':hashlib.sha256(f.read_bytes()).hexdigest()}
        (package/'manifest.json').write_text(json.dumps({'files':files}));disk=root/'disk.raw';disk.write_bytes(b'fixture');cd=root/'cd.raw';cd.write_bytes(b'fixture')
        self.cd=str(cd);self.package=package
        self.p={'schema_version':1,'id':'test','package_root':str(package),'run_dir':str(run),'cpu':'7400','ram_mb':512,'display':{'backend':'vnc','width':1024,'height':768,'depth':32},'audio':{'backend':'none'},'media':{'disk':str(disk),'disk_fixture_id':'unit-fixture','boot':'disk'},'network':'none','input':'virtio-tablet','share':{'enabled':True,'path':str(share),'tag':'Test Share'},'timing':'realtime','nvram':'volatile'}
    def test_standard_classic_video_modes_are_accepted(self):
        for width,height in [(640,480),(800,600),(1024,768)]:
            with self.subTest(width=width,height=height):
                self.p['display'].update(width=width,height=height)
                self.assertIn(f'{width}x{height}x32',machine.argv(self.p))
    def test_unknown_qemu_options_are_rejected(self):
        self.p['extra_args']=['-snapshot']
        with self.assertRaises(ValueError):machine.argv(self.p)
    def test_mixed_firmware_is_rejected(self):
        (self.package/'share/qemu/openbios-ppc').write_bytes(b'wrong')
        with self.assertRaisesRegex(ValueError,'hash mismatch'):machine.argv(self.p)
    def test_installer_does_not_export_host_share_or_accelerate_clock(self):
        self.p['media'].update({'boot':'cd','cd':self.cd,'cd_fixture_id':'unit-cd'});self.p['timing']='icount-finder-experimental'
        args=machine.argv(self.p)
        self.assertNotIn('-icount',args);self.assertNotIn('-fsdev',args)
        self.assertIn('virtio-tablet-pci',args);self.assertIn('macio-ide.dma-completion-delay-ns=1000000',args)
    def test_retroboot_display_uses_presenter_socket_and_two_qmp_monitors(self):
        self.p['display'].update(backend='retroboot',socket='/run/retroboot/display.sock')
        args=machine.argv(self.p);run=Path(self.p['run_dir']).resolve()
        self.assertIn('retroboot,socket=/run/retroboot/display.sock',args)
        self.assertNotIn('-vnc',args)
        qmp=[args[i+1] for i,a in enumerate(args) if a=='-qmp']
        self.assertEqual(qmp,[f'unix:{run}/qmp-control.sock,server=on,wait=off',f'unix:{run}/qmp-events.sock,server=on,wait=off'])
        self.assertIn('macio-ide.pio-status-drain=on',args)
    def test_display_socket_requires_retroboot_backend(self):
        self.p['display']['socket']='/run/retroboot/display.sock'
        with self.assertRaisesRegex(ValueError,'retroboot'):machine.argv(self.p)
    def test_installer_keeps_firmware_pio_path_unchanged(self):
        self.p['media'].update({'boot':'cd','cd':self.cd,'cd_fixture_id':'unit-cd'})
        self.assertNotIn('macio-ide.pio-status-drain=on',machine.argv(self.p))
    def _airport_package(self):
        m=json.loads((self.package/'manifest.json').read_text())
        m['sources']={'retroboot_patches':[{'path':'emulator/patches/airport-hermes-card.patch'}]}
        (self.package/'manifest.json').write_text(json.dumps(m))
    def test_airport_route_needs_a_package_with_the_card(self):
        self.p['network']='user-airport'
        with self.assertRaisesRegex(ValueError,'AirPort'):machine.argv(self.p)
    def test_airport_route_wires_card_bridge_and_nat(self):
        self._airport_package();self.p['network']='user-airport';self.p['airport']={'socket':'/run/retroboot/airport.sock'}
        args=machine.argv(self.p)
        self.assertEqual(args[args.index('-nic')+1],'none')
        for a in ['macio-newworld.airport=true','retroboot-airport.netdev=apnet','retroboot-airport.chardev=airport0','user,id=apnet']:
            self.assertIn(a,args)
        self.assertIn('socket,id=airport0,path=/run/retroboot/airport.sock,reconnect-ms=1000',args)
        self.assertNotIn('retroboot-airport.trace=on',args)
        self.p['airport']['trace']=True
        self.assertIn('retroboot-airport.trace=on',machine.argv(self.p))
    def test_airport_default_socket_is_in_the_run_directory(self):
        self._airport_package();self.p['network']='user-airport'
        run=Path(self.p['run_dir']).resolve()
        self.assertIn(f'socket,id=airport0,path={run}/airport.sock,reconnect-ms=1000',machine.argv(self.p))
    def test_airport_settings_need_the_airport_route(self):
        self.p['airport']={'trace':True}
        with self.assertRaisesRegex(ValueError,'user-airport'):machine.argv(self.p)
    def test_power_mac_rejects_quadras_persistent_pram(self):
        self.p['nvram']='mtd-pram'
        with self.assertRaisesRegex(ValueError,'NVRAM'):machine.argv(self.p)

if __name__=='__main__':unittest.main()
