#!/usr/bin/env python3
"""Original bare PPC write/read/poll test for MPC7400 MSSCR0 DL1HWF."""
import argparse,importlib.util,json,pathlib,struct,subprocess,time
p=argparse.ArgumentParser();p.add_argument('--negative');p.add_argument('--qemu','--positive',dest='positive',required=True);p.add_argument('--support',required=True);a=p.parse_args()
spec=importlib.util.spec_from_file_location('probe',pathlib.Path(__file__).with_name('qtest_osx_sleep.py'));m=importlib.util.module_from_spec(spec);spec.loader.exec_module(m)
original=subprocess.Popen

def probe(binary,cpu):
 def model(args,*rest,**kw):
  args=list(args);args[args.index('-cpu')+1]=cpu
  return original(args,*rest,**kw)
 subprocess.Popen=model
 try:return m.Probe(binary,a.support,False)
 finally:subprocess.Popen=original

def spr(xo,r,number):return (31<<26)|(r<<21)|((number&31)<<16)|((number>>5)<<11)|(xo<<1)

out={}
for label,binary in ([('negative',a.negative)] if a.negative else [])+[('positive',a.positive)]:
 cases=[]
 for cpu,values in [('7400',(0,0x00800000,0x60000003,0xa0804321,0xffffffff)),('7410',(0xa0804321,))]:
  for value in values:
   q=probe(binary,cpu);r=None
   try:
    r=m.begin_cpu(q);r.stop();q.wr(0x2000,b'\0'*12)
    words=[0x3c600000|(value>>16),0x60630000|(value&65535),spr(467,3,1014),spr(339,4,1014),0x90802000,0x90602004,spr(339,5,1014),0x74a60080,0x4082fff8,0x386055aa,0x90602008,0x48000000]
    q.wr(0x1100,struct.pack('>'+('I'*len(words)),*words));assert r.send('P40=00001100')=='OK';r.send('c',response=False);time.sleep(.03)
    readback,source,completion=struct.unpack('>III',q.rd(0x2000,12))
    repaired=label=='positive' and cpu=='7400';expected=value&~0x00800000 if repaired else value
    assert readback==expected,(label,cpu,hex(value),hex(readback),hex(expected))
    assert source==value,(label,cpu,'source GPR changed')
    assert completion==(0x55aa if expected&0x00800000==0 else 0),(label,cpu,hex(completion))
    cases.append({'cpu':cpu,'written':hex(value),'readback':hex(readback),'source_preserved':True,'poll_completed':completion==0x55aa})
   finally:
    if r:r.s.close()
    q.close()
 out[label]=cases
print(json.dumps(out,indent=2))
