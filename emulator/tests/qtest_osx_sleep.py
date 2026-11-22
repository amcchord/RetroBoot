#!/usr/bin/env python3
"""Drive real Core99 PMU packets through its VIA shift-register interface."""
import argparse,atexit,json,pathlib,socket,struct,subprocess,tempfile,time
CFG_ADDR,CFG_DATA=0xf2800000,0xf2c00000
class Probe:
 def __init__(self,qemu,support,enabled):
  self.closed=False;atexit.register(self.close)
  self.tmp=tempfile.TemporaryDirectory(prefix='rb-pmu-');root=pathlib.Path(self.tmp.name)
  server=socket.socket(socket.AF_UNIX);server.bind(str(root/'qtest.sock'));server.listen(1);server.settimeout(10)
  self.log=(root/'qemu.log').open('w+');args=[qemu,'-cpu','7400','-m','16','-M','mac99,via=pmu,usb=off,audiodev=snd0','-audiodev','none,id=snd0','-L',support,'-bios',str(pathlib.Path(support)/'openbios-ppc-osx'),'-S','-display','none','-vga','none','-nodefaults','-serial','none','-monitor','none','-gdb','unix:'+str(root/'gdb.sock')+',server=on,wait=off','-qtest','unix:'+str(root/'qtest.sock'),'-qmp','unix:'+str(root/'qmp.sock')+',server=on,wait=off']
  if enabled:args+=['-global','via-pmu.osx-sleep=on']
  self.proc=subprocess.Popen(args,stdin=subprocess.DEVNULL,stdout=self.log,stderr=self.log)
  self.sock,_=server.accept();server.close();self.sock.settimeout(10);self.f=self.sock.makefile('rw')
  self.qmp=socket.socket(socket.AF_UNIX);self.qmp.settimeout(10);self.qmp.connect(str(root/'qmp.sock'));self.qf=self.qmp.makefile('rw');json.loads(self.qf.readline());self.execute('qmp_capabilities');self.events=[];self.root=root
  slots=[]
  for slot in range(32):
   ident=self.cfg(slot,0)
   if ident&65535==0x106b:slots.append((slot,hex(ident)))
  macios=[slot for slot,ident in slots if int(ident,16)>>16 in (0x22,0x25,0x17)]
  if len(macios)!=1:raise RuntimeError('Unexpected MacIO identity:'+repr(slots))
  self.slot=macios[0];self.cfg(self.slot,0x10,0x80000000);self.cfg(self.slot,4,2);self.base=0x80016000
  self.wreg(2,0x10);self.wreg(0,0x10)
  assert self.rreg(0)&0x18==0x18,('PMU handshake not idle',self.slot,hex(self.cfg(self.slot,0x10)),hex(self.rreg(0)))
 def command(self,line):
  self.f.write(line+'\n');self.f.flush()
  while True:
   r=self.f.readline().strip()
   if r.startswith('OK'):return r.split()
   if r.startswith(('FAIL','ERR')):raise RuntimeError(r)
   if not r:raise EOFError('qtest ended')
 def wr(self,addr,data):self.command('write 0x%x %d 0x%s'%(addr,len(data),data.hex()))
 def rd(self,addr,n):return bytes.fromhex(self.command('read 0x%x %d'%(addr,n))[1][2:])
 def cfg(self,slot,offset,value=None):
  self.wr(CFG_ADDR,struct.pack('<I',0x80000000|(slot<<11)|offset))
  if value is None:return struct.unpack('<I',self.rd(CFG_DATA,4))[0]
  self.wr(CFG_DATA,struct.pack('<I',value))
 def rreg(self,index):return self.rd(self.base+(index<<9),1)[0]
 def wreg(self,index,value):self.wr(self.base+(index<<9),bytes([value]))
 def byte(self,value=None):
  if value is not None:self.wreg(10,value)
  self.wreg(0,0);assert self.rreg(0)&0x18==0
  answer=self.rreg(10)
  self.wreg(0,0x10);assert self.rreg(0)&0x18==0x18
  return answer
 def pmu(self,command,data=(),variable=False,count=0):
  self.wreg(11,0x10);self.byte(command)
  for b in data:self.byte(b)
  if not variable and not count:return []
  self.wreg(11,0)
  if variable:count=self.byte()
  assert count<128,count
  return [self.byte() for _ in range(count)]
 def ack(self):return self.pmu(0x78,variable=True)
 def step(self,ms):time.sleep(ms/1000)  # real-time TCG; no virtual clock override
 def execute(self,name,args=None):
  r={'execute':name}
  if args:r['arguments']=args
  self.qf.write(json.dumps(r)+'\n');self.qf.flush()
  while True:
   value=json.loads(self.qf.readline())
   if 'return' in value:return value['return']
   if 'error' in value:raise RuntimeError(value)
 def close(self):
  if self.closed:return
  self.closed=True
  for name in ['qf','qmp','f','sock']:
   item=getattr(self,name,None)
   if item:item.close()
  proc=getattr(self,'proc',None)
  if proc and proc.poll() is None:proc.terminate();proc.wait(5)
  if getattr(self,'log',None):self.log.close()
  if getattr(self,'tmp',None):self.tmp.cleanup()

class RSP:
    def __init__(self, path):
        self.s = socket.socket(socket.AF_UNIX)
        self.s.settimeout(5)
        self.s.connect(str(path))

    def receive(self):
        while True:
            value = self.s.recv(1)
            if not value:
                raise EOFError('QEMU GDB connection closed')
            if value == b'$':
                break
        data = b''
        while True:
            c = self.s.recv(1)
            if not c:
                raise EOFError('QEMU GDB packet truncated')
            if c == b'#':
                break
            data += c
        check = self.s.recv(2)
        assert int(check, 16) == sum(data) % 256
        self.s.sendall(b'+')
        return data.decode()

    def send(self, data, response=True):
        b = data.encode()
        self.s.sendall(b'$' + b + b'#' + ('%02x' % (sum(b) % 256)).encode())
        assert self.s.recv(1) == b'+'
        return self.receive() if response else None

    def stop(self):
        self.s.sendall(b'\x03')
        reply = self.receive()
        assert reply.startswith(('S', 'T')), reply


def state(q):
    return {name: q.execute('qom-get', {'path': 'pmu', 'property': 'osx-' + name})
            for name in ('sleep-pending', 'asleep', 'wake-latched')}


def registers(q):
    return q.execute('human-monitor-command', {'command-line': 'info registers'})


def wait_for(q, expected, timeout=2):
    end = time.monotonic() + timeout
    while time.monotonic() < end:
        q.step(10)
        time.sleep(.01)
        actual = state(q)
        if all(actual[k] == v for k, v in expected.items()):
            return actual
    raise AssertionError((expected, state(q), registers(q)))


def key(q, down=True):
    q.execute('input-send-event', {'events': [{'type': 'key', 'data': {
        'down': down, 'key': {'type': 'qcode', 'data': 'shift'}}}]})


def begin_cpu(q):
    r = RSP(q.root/'gdb.sock')
    r.send('qSupported:multiprocess+;xmlRegisters=ppc')
    q.wr(0x100, struct.pack('>I', 0x48000000))
    q.wr(0x1000, struct.pack('>I', 0x48000000))
    assert r.send('P40=00001000') == 'OK'
    assert r.send('P41=00001000') == 'OK'  # ME only; not hardware sleep
    r.send('c', response=False)
    time.sleep(.03)
    return r


def enter_power(q, r, vector=0x100, hid0=0x00200000):
    r.stop()
    # Original bare PPC sequence. The guest writes its own physical wake
    # vector, moves DEC out of range, then executes HID0 SLEEP + MSR POW.
    words = [0x38600000 | vector, 0x90600080,
             0x3c800000 | 0x7fff, 0x6084ffff,
             0x7c9603a6,                       # mtdec r4
             0x3c600000 | (hid0 >> 16),
             0x60630000 | (hid0 & 0xffff),
             0x7c70fba6,                       # mtspr HID0,r3
             0x3c600004, 0x60631000,            # POW | ME
             0x7c600124, 0x4c00012c, 0x48000000]
    q.wr(0x1100, struct.pack('>' + 'I'*len(words), *words))
    assert r.send('P40=00001100') == 'OK'
    assert r.send('P41=00001000') == 'OK'
    r.send('c', response=False)


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument('--qemu', required=True)
    parser.add_argument('--support', required=True)
    args = parser.parse_args()
    results = {}

    q = Probe(args.qemu, args.support, False)
    try:
        assert q.pmu(0x7f, b'MATT', count=1) == [0]
        assert not any(state(q).values())
        results['default_off_legacy_reply'] = True
    finally:
        q.close()

    for old_vector in (0, 0x100):
        q = Probe(args.qemu, args.support, True)
        try:
            r = begin_cpu(q)
            q.wr(0x80, struct.pack('>I', old_vector))
            assert q.pmu(0x7f, b'FAIL', count=1) == [0]
            assert not any(state(q).values())
            assert q.pmu(0x7f, b'MATT', count=1) == [0x70]
            assert state(q)['sleep-pending'] and not state(q)['asleep']
            key(q)
            q.step(100)
            time.sleep(.06)
            assert state(q) == {'sleep-pending': True, 'asleep': False,
                                'wake-latched': True}, state(q)
            assert 'NIP 00001000' in registers(q), registers(q)
            # Repeated packets do not discard the early wake.
            assert q.pmu(0x7f, b'MATT', count=1) == [0x70]
            assert state(q)['wake-latched']
            enter_power(q, r)
            wait_for(q, {'sleep-pending': False, 'asleep': False,
                         'wake-latched': False})
            regs = registers(q)
            assert 'NIP 00000100' in regs, regs
            assert 'MSR 00001000' in regs, regs
            results['early_key_%s_vector_latches_until_hardware_sleep' %
                    ('zero' if old_vector == 0 else 'stale')] = True
            r.s.close()
        finally:
            q.close()

    q = Probe(args.qemu, args.support, True)
    try:
        r = begin_cpu(q)
        assert q.pmu(0x7f, b'MATT', count=1) == [0x70]
        enter_power(q, r, hid0=0x00400000)  # actual NAP is not system SLEEP
        key(q)
        q.step(100)
        time.sleep(.06)
        assert state(q) == {'sleep-pending': True, 'asleep': False,
                            'wake-latched': True}, state(q)
        assert 'NIP 00000100' not in registers(q), registers(q)
        results['nap_pow_is_not_accepted_system_sleep'] = True
        r.s.close()
    finally:
        q.close()

    for invalid in (None, 0, 0x2000, 0x101):
        q = Probe(args.qemu, args.support, True)
        try:
            r = begin_cpu(q)
            assert q.pmu(0x7f, b'MATT', count=1) == [0x70]
            enter_power(q, r, vector=0x100 if invalid is None else invalid)
            wait_for(q, {'sleep-pending': False, 'asleep': True,
                         'wake-latched': False})
            regs = registers(q)
            assert 'MSR 00041000' in regs and 'HID0 00200000' in regs, regs
            q.step(1100)  # pending periodic PMU interrupt remains preserved
            key(q)
            if invalid is not None:
                q.step(50)
                time.sleep(.05)
                assert state(q)['asleep'] and state(q)['wake-latched'], state(q)
                assert 'NIP 00000100' not in registers(q), registers(q)
                q.wr(0x80, struct.pack('>I', 0x100))
                key(q)
            wait_for(q, {'sleep-pending': False, 'asleep': False,
                         'wake-latched': False})
            assert 'NIP 00000100' in registers(q), registers(q)
            assert q.ack()[0] & 0x80, 'Periodic interrupt was lost across wake'
            results['hardware_sleep_wake' if invalid is None else
                    'invalid_vector_%x_keeps_wake_armed' % invalid] = True
            # Reset pending/latched state and invalidate any queued CPU check.
            assert q.pmu(0x7f, b'MATT', count=1) == [0x70]
            key(q)
            q.execute('system_reset')
            time.sleep(.05)
            assert not any(state(q).values()), state(q)
            assert 'NIP 00000100' not in registers(q), registers(q)
            results['reset_invalidates_pending_wake'] = True
            r.s.close()
        finally:
            q.close()
    print(json.dumps(results, indent=2))


if __name__ == '__main__':
    main()
