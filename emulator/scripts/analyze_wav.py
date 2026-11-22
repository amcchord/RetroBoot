#!/usr/bin/env python3
"""Inspect retained PCM; optionally create a playable copy after QEMU has stopped."""
import argparse,array,hashlib,json,struct,sys,wave
from pathlib import Path
ap=argparse.ArgumentParser();ap.add_argument('capture',type=Path);ap.add_argument('--normalized-copy',type=Path);args=ap.parse_args()
raw=args.capture.read_bytes()
if len(raw)<44 or raw[:4]!=b'RIFF' or raw[8:16]!=b'WAVEfmt ' or raw[36:40]!=b'data':raise SystemExit('Expected QEMU PCM WAV layout')
fmt,channels,rate,byte_rate,alignment,bits=struct.unpack_from('<HHIIHH',raw,20)
if fmt!=1 or bits!=16 or channels not in [1,2] or alignment!=channels*2 or byte_rate!=rate*alignment:raise SystemExit('Unsupported or inconsistent PCM format')
data=raw[44:]
if len(data)%alignment:raise SystemExit('Truncated PCM frame')
samples=array.array('h',data)
if sys.byteorder!='little':samples.byteswap()
result={'sha256':hashlib.sha256(raw).hexdigest(),'channels':channels,'sample_rate':rate,'bits':bits,'frames':len(data)//alignment,'nonzero_samples':sum(x!=0 for x in samples),'peak_absolute':max(map(abs,samples),default=0),'header_finalized':struct.unpack_from('<I',raw,4)[0]==len(raw)-8 and struct.unpack_from('<I',raw,40)[0]==len(data)}
if args.normalized_copy:
    if args.normalized_copy.resolve()==args.capture.resolve():raise SystemExit('Retain raw capture; normalized copy must use a different path')
    with wave.open(str(args.normalized_copy),'wb') as out:out.setnchannels(channels);out.setsampwidth(bits//8);out.setframerate(rate);out.writeframes(data)
    result['normalized_copy_sha256']=hashlib.sha256(args.normalized_copy.read_bytes()).hexdigest()
print(json.dumps(result,indent=2))
