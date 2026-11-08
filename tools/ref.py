# Python 参照実装 (途中経過つき)
import struct
from minercss import K, H0
r = lambda x, n: ((x >> n) | (x << (32 - n))) & 0xffffffff
def comp(st, blk, rounds=64, trace=None):
    w = list(struct.unpack('>16I', blk))
    for t in range(16, 64):
        w.append(((r(w[t-2],17)^r(w[t-2],19)^(w[t-2]>>10)) + w[t-7] + (r(w[t-15],7)^r(w[t-15],18)^(w[t-15]>>3)) + w[t-16]) & 0xffffffff)
    a,b,c,d,e,f,g,h = st
    for t in range(rounds):
        t1 = (h + (r(e,6)^r(e,11)^r(e,25)) + ((e&f)^(~e&g)) + K[t] + w[t]) & 0xffffffff
        t2 = ((r(a,2)^r(a,13)^r(a,22)) + ((a&b)^(a&c)^(b&c))) & 0xffffffff
        h,g,f,e,d,c,b,a = g,f,e,(d+t1)&0xffffffff,c,b,a,(t1+t2)&0xffffffff
        if trace is not None: trace.append((a, e))
    return [(x+y)&0xffffffff for x,y in zip(st,[a,b,c,d,e,f,g,h])], w
