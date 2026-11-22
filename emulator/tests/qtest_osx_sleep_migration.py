#!/usr/bin/env python3
"""PMU protocol/migration checks using original bare-PPC controls."""
import argparse, importlib.util, json, pathlib, subprocess, time
p=argparse.ArgumentParser();p.add_argument('--qemu',required=True);p.add_argument('--support',required=True);a=p.parse_args()
spec=importlib.util.spec_from_file_location('probe',pathlib.Path(__file__).with_name('qtest_osx_sleep.py'));m=importlib.util.module_from_spec(spec);spec.loader.exec_module(m)
original_popen=subprocess.Popen

def receiver(enabled=True):
    def incoming(args,*rest,**kw):
        return original_popen(args+['-incoming','defer'],*rest,**kw)
    subprocess.Popen=incoming
    try:return m.Probe(a.qemu,a.support,enabled)
    finally:subprocess.Popen=original_popen

def migrated(q):
    deadline=time.monotonic()+8
    while time.monotonic()<deadline:
        s=q.execute('query-migrate')
        if s.get('status')=='completed':return
        if s.get('status')=='failed':raise AssertionError(s)
        time.sleep(.02)
    raise AssertionError(q.execute('query-migrate'))

out={}
for phase in ('pending','asleep'):
    q=m.Probe(a.qemu,a.support,True);dst=None;r=None
    try:
        r=m.begin_cpu(q)
        assert q.pmu(0x7f,b'MATT',count=1)==[0x70]
        if phase=='pending':
            m.key(q);m.wait_for(q,{'sleep-pending':True,'asleep':False,'wake-latched':True})
        else:
            m.enter_power(q,r);m.wait_for(q,{'sleep-pending':False,'asleep':True,'wake-latched':False})
        before=m.state(q)
        image=q.root/'migration.bin'
        q.execute('migrate',{'uri':'exec:cat > '+str(image)})
        migrated(q)
        dst=receiver()
        dst.execute('migrate-incoming',{'uri':'exec:cat '+str(image)})
        migrated(dst)
        assert m.state(dst)==before,(before,m.state(dst))
        dst.execute('cont')
        if phase=='pending':
            r2=m.RSP(dst.root/'gdb.sock');r2.send('qSupported:multiprocess+;xmlRegisters=ppc')
            r2.send('c',response=False);time.sleep(.03)
            m.enter_power(dst,r2)
        else:m.key(dst)
        m.wait_for(dst,{'sleep-pending':False,'asleep':False,'wake-latched':False})
        assert 'NIP 00000100' in m.registers(dst),m.registers(dst)
        out[phase+'_roundtrip_and_wake']=True
    finally:
        if r:r.s.close()
        if dst:dst.close()
        q.close()
print(json.dumps(out,indent=2))
