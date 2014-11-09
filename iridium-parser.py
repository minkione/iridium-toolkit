#!/usr/bin/env python
# vim: set ts=4 sw=4 tw=0 et pm=:
import sys
import re
from fec import stringify, listify
from bch import divide, repair
import fileinput
import getopt
import types
import copy

from itertools import izip
def grouped(iterable, n):
    "s -> (s0,s1,s2,...sn-1), (sn,sn+1,sn+2,...s2n-1), (s2n,s2n+1,s2n+2,...s3n-1), ..."
    return izip(*[iter(iterable)]*n)


options, remainder = getopt.getopt(sys.argv[1:], 'vi:', [
                                                         'verbose',
                                                         'input',
                                                         ])
iridium_access="001100000011000011110011" # Actually 0x789h in BPSK
iridium_lead_out="100101111010110110110011001111"
header_messaging="00110011111100110011001111110011"
messaging_bch_poly=1897

verbose = False
input= "raw"

for opt, arg in options:
    if opt in ('-v', '--verbose'):
        verbose = True
    elif opt in ('-i', '--input'):
        short=arg

class ParserError(Exception):
    pass
        
class Message(object):
    def __init__(self,line):
        p=re.compile('RAW: ([^ ]*) (\d+) (\d+) A:(\w+) L:(\w+) +(\d+)% ([\d.]+) +(\d+) ([\[\]<> 01]+)(.*)')
        m=p.match(line)
        if(not m):
            raise Exception("did not match")
        self.filename=m.group(1)
        self.timestamp=int(m.group(2))
        self.frequency=int(m.group(3))
#        self.access_ok=(m.group(4)=="OK")
#        self.leadout_ok=(m.group(5)=="OK")
        self.confidence=int(m.group(6))
        self.level=float(m.group(7))
#        self.raw_length=m.group(8)
        self.bitstream_raw=re.sub("[\[\]<> ]","",m.group(9)) # raw string
        self.error=False
        self.error_msg=[]
        if m.group(10):
            self.extra_data=m.group(10)
            self._new_error("There is crap at the end in extra_data")
    def upgrade(self):
        if self.error: return self
        if(self.bitstream_raw.startswith(iridium_access)):
            return IridiumMessage(self).upgrade()
        return self
    def _new_error(self,msg):
        self.error=True
        msg=str(type(self).__name__) + ": "+msg
        if not self.error_msg or self.error_msg[-1] != msg:
            self.error_msg.append(str(type(self).__name__) + ": "+msg)
    def _pretty_header(self):
       return "%s %07d %010d %3d%% %.3f"%(self.filename,self.timestamp,self.frequency,self.confidence,self.level)
    def _pretty_trailer(self):
        return ""
    def pretty(self):
       str= "MSG: "+self._pretty_header()+" "+self.bitstream_raw
       if("extra_data" in self.__dict__):
            str+=" "+self.extra_data
       str+=self._pretty_trailer()
       return str

class IridiumMessage(Message):
    def __init__(self,msg):
        self.__dict__=copy.deepcopy(msg.__dict__)
        data=self.bitstream_raw[len(iridium_access):]
        self.header=data[:32]
        data=data[32:]
        m=re.compile('(\d{64})').findall(data)
        self.bitstream_descrambled=""
        for (group) in m:
            self.bitstream_descrambled+=de_interleave(group)
        if(not self.bitstream_descrambled):
            self._new_error("No data to descramble")
        data=data[len(self.bitstream_descrambled):]
        self.lead_out_ok= data.startswith(iridium_lead_out)
        if(data):
            self.descramble_extra=data
        else:
            self.descramble_extra=""
    def upgrade(self):
        if self.error: return self
        if(self.header == header_messaging):
            try:
                return IridiumMessagingMessage(self).upgrade()
            except ParserError,e:
                self._new_error(str(e))
                return self
        return self
    def _pretty_header(self):
        str= super(IridiumMessage,self)._pretty_header()
        return str+ " len:%03d"%(len(self.header+self.bitstream_descrambled+self.descramble_extra)/2)+" L:"+("no","OK")[self.lead_out_ok]+" "+self.header
    def _pretty_trailer(self):
        str= super(IridiumMessage,self)._pretty_trailer()
        lead_out_index = self.descramble_extra.find(iridium_lead_out)
        if(lead_out_index>=0):
            data=self.descramble_extra[:lead_out_index]+"["+self.descramble_extra[lead_out_index:lead_out_index+len(iridium_lead_out)]+"]"  +self.descramble_extra[lead_out_index+len(iridium_lead_out):]
        else:
            data=self.descramble_extra
        return str+ " descr_extra:"+data
    def pretty(self):
       str= "IRI: "+self._pretty_header()+" "+self.bitstream_descrambled
       str+=self._pretty_trailer()
       return str

class IridiumMessagingMessage(IridiumMessage):
    def __init__(self,imsg):
        self.__dict__=copy.deepcopy(imsg.__dict__)
        poly="{0:011b}".format(messaging_bch_poly);
        self.bitstream_messaging=""
        self.oddbits=""
        self.fixederrs=0
        m=re.compile('(\d)(\d{20})(\d{10})(\d)').findall(self.bitstream_descrambled)
        # TODO: bch_ok and parity_ok arrays
        for (odd,msg,bch,parity) in m:
            (errs,bnew)=repair(poly, odd+msg+bch)
            if(errs>0):
                self.fixederrs+=1
                odd=bnew[0]
                msg=bnew[1:21]
                bch=bnew[21:]
            if(errs<0):
                self._new_error("BCH decode failed")
                raise ParserError("BCH decode failed")
            parity=len(re.sub("0","",odd+msg+bch+parity)) %2
            if parity==1:
                self._new_error("Parity error")
                raise ParserError("Parity error")
            self.bitstream_messaging+=msg
            self.oddbits+=odd
        rest=self.bitstream_messaging
        # There is an unparsed 20-bit header
        self.msg_header=rest[0:20]
        # If oddbits ends in 1, this is an all-1 block -- remove it
        self.msg_trailer=""
        if(self.oddbits[-1]=="1"):
            self.msg_trailer=rest[-20:]
            if(self.msg_trailer != "1"*20):
                self._new_error("trailer exists, but not all-1")
            rest=rest[0:-20]
            # If oddbits still ends in 1, probably also an all-1 block
            if(self.oddbits[-2]=="1"):
                self.msg_trailer=rest[-20:]+self.msg_trailer
                if(self.msg_trailer != "1"*40):
                    self._new_error("second trailer exists, but not all-1")
                rest=rest[0:-20]
        # If oddbits starts with 1, there is a 80-bit "pre" message
        if self.oddbits[0]=="1":
            self.msg_pre=rest[20:100]
            rest=rest[100:]
        else:
            self.msg_pre=""
            rest=rest[20:]
        # If enough  bits are left, there will be a pager message
        if len(rest)>20:
            self.msg_ric=int(rest[0:22][::-1],2)
            self.msg_format=int(rest[22:27],2)
            self.msg_data=rest[27:]
    def upgrade(self):
        if self.error: return self
        if("msg_format" in self.__dict__):
            if(self.msg_format == 5):
                try:
                    return IridiumMessagingAscii(self).upgrade()
                except ParserError,e:
                    self._new_error(str(e))
                    return self
        return self
    def _pretty_header(self):
        str= super(IridiumMessagingMessage,self)._pretty_header()
        if("msg_format" in self.__dict__):
            return str+ " odd:%-26s %s %-80s ric:%07d fmt:%02d"%(self.oddbits,self.msg_header,self.msg_pre,self.msg_ric,self.msg_format)
        else:
            return str+ " odd:%-26s %s %-80s"%(self.oddbits,self.msg_header,self.msg_pre)
    def _pretty_trailer(self):
        return super(IridiumMessagingMessage,self)._pretty_trailer()
    def pretty(self):
        str= "IMS: "+self._pretty_header()
        if("msg_format" in self.__dict__):
            str+= " "+self.msg_data
        str+=self._pretty_trailer()
        return str
        
class IridiumMessagingAscii(IridiumMessagingMessage):
    def __init__(self,immsg):
        self.__dict__=copy.deepcopy(immsg.__dict__)
        rest=self.msg_data
        self.msg_seq=int(rest[0:6],2)
        self.msg_zero1=int(rest[6:10],2)
        if(self.msg_zero1 != 0):
            self._new_error("zero1 is not all-zero")
        self.msg_unknown1=rest[10:20]
        self.msg_len_bit=rest[20]
        rest=rest[21:]
        if(self.msg_len_bit=="1"):
            lfl=int(rest[0:4],2)
            self.msg_len_field_len=lfl
            if(lfl == 0):
                raise ParserError("len_field_len unexpectedly 0")
            self.msg_ctr=    int(rest[4:4+lfl],2)
            self.msg_ctr_max=int(rest[4+lfl:4+lfl*2],2)
            rest=rest[4+lfl*2:]
            if(lfl<1 or lfl>2):
                self._new_error("len_field_len not 1 or 2")
        else:
            self.msg_len=0
            self.msg_ctr=0
            self.msg_ctr_max=0
        self.msg_zero2=rest[0]
        if(self.msg_zero2 != "0"):
            self._new_error("zero2 is not zero")
        self.msg_checksum=rest[1:8]
        self.msg_msgdata=rest[8:]
        m=re.compile('(\d{7})').findall(self.msg_msgdata)
        self.msg_ascii=""
        end=0
        for (group) in m:
            character = int(group, 2)
            if(character==3):
                end=1
            elif(end==1):
                self._new_error("ETX inside ascii")
            if(character<32 or character==127):
                self.msg_ascii+="[%d]"%character
            else:
                self.msg_ascii+=chr(character)
        if len(self.msg_msgdata)%7:
            self.msg_rest=self.msg_msgdata[-(len(self.msg_msgdata)%7):]
        else:
            self.msg_rest=""
        #TODO: maybe checksum checks
    def upgrade(self):
        if self.error: return self
        return self
    def _pretty_header(self):
        str= super(IridiumMessagingAscii,self)._pretty_header()
        return str+ " seq:%02d %10s %1d/%1d"%(self.msg_seq,self.msg_unknown1,self.msg_ctr,self.msg_ctr_max)
    def _pretty_trailer(self):
        return super(IridiumMessagingAscii,self)._pretty_trailer()
    def pretty(self):
       str= "MSG: "+self._pretty_header()+" %-65s"%self.msg_ascii+" +%-6s"%self.msg_rest+self._pretty_trailer()
       return str
        

"""

    def prettyprint(self):
        if self.isa=="raw":
            print "RW: "
            print "%s %d %d"%(self.filename,self,timestamp,self.frequency)
            print " %s"%self.raw
        elif self.isa=="iridium":
            print "IR: "
            print "%s %d %d"%(self.filename,self,timestamp,self.frequency)
            print "%s %s"%(self.messagetype,self.data)
"""

def de_interleave(group):
    symbols = [''.join(symbol) for symbol in grouped(group, 2)]
    even = ''.join([symbols[x] for x in range(len(symbols)-2,-1, -2)])
    odd  = ''.join([symbols[x] for x in range(len(symbols)-1, 0, -2)])
    field = odd + even
    return field

for line in fileinput.input(remainder):
    line=line.strip()
    q=Message(line)
    q=q.upgrade()
    if(q.error):
        print q.pretty()+" ERR:"+", ".join(q.error_msg)
    else:
        print q.pretty()

def objprint(q):
    for i in dir(q):
        attr=getattr(q,i)
        if i.startswith('_'):
            continue
        if isinstance(attr, types.MethodType):
            continue
        print "%s: %s"%(i,attr)