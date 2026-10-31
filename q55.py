#!/usr/bin/env python3

# q55 : Alveo U55C board access queue manager , runs on the machine hosting the board
#
#   q55 server                                          run the queue manager (enics account , forever)
#   q55 prog   <bitstream> [-t <minutes>]               queue a bitstream load request
#   q55 access <bitstream> [-t <minutes>] [-- <cmd>]    queue an access request , exclusive board access , loads the bitstream if needed
#   q55 status                                          queue status
#   q55 release                                         end your turn : release your board access , end your bitstream
#                                                       allocation window , cancel your waiting access requests
#   q55 shutdown                                        graceful queue manager shutdown (enics account)
#
# See README.md

import os
import sys
import pwd
import json
import time
import shlex
import socket
import struct
import signal
import shutil
import argparse
import tempfile
import threading
import subprocess
from collections import deque
from datetime import datetime

#----------------------------------------------------------------------------------------

Q55_DIR    = os.environ.get('Q55_DIR','/tmp/q55')             # socket and bitstream spool
SOCK_PATH  = Q55_DIR + '/q55.sock'
SPOOL_DIR  = Q55_DIR + '/spool'
MGR_USER   = os.environ.get('Q55_MGR_USER','enics')           # clients only trust a manager run by this user
HERE       = os.path.dirname(os.path.abspath(__file__))
PROG_CMD   = os.environ.get('Q55_PROG_CMD', HERE + '/program.sh')   # called with the bitstream path
NEED_SUDO  = 'Q55_PROG_CMD' not in os.environ                 # program.sh needs the manager's sudo
STATE_FILE = os.path.expanduser(os.environ.get('Q55_STATE','~/.q55_state.json'))
LOG_FILE   = os.path.expanduser(os.environ.get('Q55_LOG','~/.q55.log'))
GRACE_SEC  = float(os.environ.get('Q55_GRACE_SEC','60'))      # warning before an access session is revoked
DEF_MIN    = 10                                               # default allocation minutes
LOAD_TIMEOUT_SEC = 900
SUDO_KEEPALIVE_SEC = 240

#----------------------------------------------------------------------------------------

def log(msg) :
    line = datetime.now().strftime('%Y-%m-%d %H:%M:%S  ') + msg
    print(line, flush=True)
    try :
       with open(LOG_FILE,'a') as f : f.write(line + '\n')
    except OSError :
       pass

# The manager times everything with time.monotonic() , so that a system clock change (NTP , date)
# can't stretch or cut an allocation window ; the wall clock is used only to show times.

def mono_to_wall(t) :
    return t + time.time() - time.monotonic()

def wall_to_mono(t) :
    return t - time.time() + time.monotonic()

def hhmm(t) :
    # t is a time.monotonic() value
    return datetime.fromtimestamp(mono_to_wall(t)).strftime('%H:%M')

def dur(sec) :
    sec = max(0,int(sec))
    return '%d:%02d' % (sec//60, sec%60)

def send(conn, mtype, msg='', **kw) :
    # One JSON message per line ; a vanished client is ignored (its handler cleans up)
    try :
       conn.sendall((json.dumps(dict(type=mtype, msg=msg, **kw)) + '\n').encode())
    except OSError :
       pass

def close_conn(conn) :
    try :
       conn.shutdown(socket.SHUT_RDWR)
    except OSError :
       pass

class Refused(Exception) :
    pass

#========================================================================================
# Queue manager
#========================================================================================

class Mgr :

   def __init__(self) :
      self.cv      = threading.Condition()
      self.active  = None    # loaded bitstream {path ver snap user t loaded_at}
      self.loading = None    # load entry being programmed
      self.loads   = []      # pending loads {path ver snap user t req_at}
      self.holder  = None    # access entry holding the board
      self.waiters = []      # pending access {id user path t conn req_at granted_at warned_at}
      self.events  = deque(maxlen=8)
      self.stopping = False
      self.done     = False
      self.seq      = 0
      self.started  = time.time()
      self.restore_state()

   #-------------------------------------------------------------------------------------

   def event(self, msg) :
      log(msg)
      self.events.append(hhmm(time.monotonic()) + '  ' + msg)

   def window_left(self) :
      return self.active['loaded_at'] + self.active['t']*60 - time.monotonic() if self.active else 0

   def load_due(self) :
      # A pending load replaces the active bitstream once its allocation window is over ,
      # or at once when the user who loaded the active bitstream asks for it and nobody else holds
      # or waits for that bitstream
      if not self.loads :
         return False
      a = self.active
      if a is None or self.window_left() <= 0 :
         return True
      others = [w for w in self.waiters + [self.holder] if w and w['path'] == a['path'] and w['user'] != a['user']]
      return self.loads[0]['user'] == a['user'] and not others

   def same_file(self, e, path, ver) :
      return e is not None and e['path'] == path and e['ver'] == ver

   def loading_msg(self) :
      return 'Loading %s now , this can take a minute or two' % self.loading['path']

   #-------------------------------------------------------------------------------------

   def save_state(self) :
      try :
         # The state file keeps wall clock time , monotonic time is meaningless in another run
         a = self.active and dict(self.active, loaded_at=mono_to_wall(self.active['loaded_at']))
         with open(STATE_FILE,'w') as f : json.dump(a,f)
      except OSError as x :
         log('WARNING: cannot save state: %s' % x)

   def restore_state(self) :
      try :
         with open(STATE_FILE) as f : a = json.load(f)
      except (OSError,ValueError) :
         return
      if a :
         a['ver'] = tuple(a['ver'])
         a['restored'] = True
         if not os.path.exists(a.get('snap') or '') : a['snap'] = None
         # A load time in the future means the clock was set back since , count from now
         a['loaded_at'] = wall_to_mono(min(a['loaded_at'],time.time()))
         self.active = a
         log('Restored from the previous run: %s (loaded by %s at %s) , not verified on the board'
             % (a['path'],a['user'],hhmm(a['loaded_at'])))

   def cleanup_spool(self) :
      used = set(e['snap'] for e in [self.active,self.loading] + self.loads if e and e['snap'])
      for f in os.listdir(SPOOL_DIR) :
         p = SPOOL_DIR + '/' + f
         if not f.endswith('.part') and p not in used :
            os.remove(p)

   #-------------------------------------------------------------------------------------
   # Load requests (q55 prog , and q55 access when the bitstream is not active or queued)

   def decide(self, user, path, ver, t, snap) :
      # Returns (message , need_snapshot). Called with the lock held.
      if self.stopping :
         raise Refused('the queue manager is shutting down')
      a = self.active
      if self.same_file(a,path,ver) :
         w = self.window_left()
         return ('ACTIVE: %s is loaded (by %s at %s , %s)' % (path,a['user'],hhmm(a['loaded_at']),
                 'allocation window %s left' % dur(w) if w > 0 else 'allocation window over'), False)
      if self.loading and self.loading['path'] == path and self.loading['ver'] == ver :
         return ('LOADING: %s is being loaded now' % path, False)
      for i,e in enumerate(self.loads) :
         if e['path'] == path :
            if e['ver'] == ver :
               return ('PENDING: %s is already in the load queue at position %d' % (path,i+1), False)
            if snap is None : return (None, True)
            e['ver'], e['snap'] = ver, snap
            return ('PENDING: %s is in the load queue at position %d , updated to the newer file' % (path,i+1), False)
      if snap is None : return (None, True)
      changed = ' (changed since it was loaded)' if a and a['path'] == path else ''
      self.loads.append(dict(path=path, ver=ver, snap=snap, user=user, t=t, req_at=time.monotonic()))
      self.event('%s queued a load of %s%s' % (user,path,changed))
      self.cv.notify_all()
      pos = len(self.loads)
      if self.loading is None and self.holder is None and pos == 1 and self.load_due() :
         return ('QUEUED: %s%s , loading starts now' % (path,changed), False)
      return ('BUSY: the board is allocated , your load of %s%s is queued at position %d\n\n%s'
              % (path,changed,pos,self.status_text()), False)

   def request_load(self, user, path, fd, t) :
      st = os.fstat(fd)
      ver = (st.st_size, st.st_mtime_ns)
      with self.cv :
         msg, need = self.decide(user,path,ver,t,None)
         if not need :
            return msg
         self.seq += 1
         snap = '%s/%d_%s' % (SPOOL_DIR,self.seq,os.path.basename(path))
      # Copy the bitstream into the spool (the manager can't read user files by path)
      with os.fdopen(os.dup(fd),'rb') as src, open(snap + '.part','wb') as dst :
         src.seek(0)
         shutil.copyfileobj(src,dst,1<<20)
      os.rename(snap + '.part',snap)
      with self.cv :
         msg, need = self.decide(user,path,ver,t,snap)
         self.cleanup_spool()  # drops the copy if someone else queued the same file meanwhile
      return msg

   #-------------------------------------------------------------------------------------

   def run_load(self, e) :
      log('Loading %s (requested by %s) ...' % (e['path'],e['user']))
      ok, out = False, ''
      try :
         if NEED_SUDO and subprocess.call(['sudo','-n','true'],stdin=subprocess.DEVNULL,
                                          stdout=subprocess.DEVNULL,stderr=subprocess.DEVNULL) != 0 :
            out = 'the queue manager sudo credential expired , the manager must be restarted'
         else :
            p = subprocess.run(shlex.split(PROG_CMD) + [e['snap']],stdin=subprocess.DEVNULL,
                               stdout=subprocess.PIPE,stderr=subprocess.STDOUT,text=True,timeout=LOAD_TIMEOUT_SEC)
            out, ok = p.stdout, p.returncode == 0
      except Exception as x :
         out = str(x)
      with self.cv :
         self.loading = None
         if ok :
            self.active = dict(e, loaded_at=time.monotonic())
            self.save_state()
            self.event('Loaded %s (requested by %s , allocation %g min)' % (e['path'],e['user'],e['t']))
            for w in self.waiters :
               if w['path'] == e['path'] :
                  send(w['conn'],'info','%s is loaded , waiting for board access' % e['path'])
         else :
            log('Load output:\n' + out.rstrip())
            tail = '\n'.join(out.strip().splitlines()[-5:])
            self.event('LOAD FAILED: %s (requested by %s)' % (e['path'],e['user']))
            # The board state is unknown after a failed load
            self.active = None
            self.save_state()
            for w in [w for w in self.waiters if w['path'] == e['path']] :
               send(w['conn'],'error','load of %s failed:\n%s' % (e['path'],tail))
               close_conn(w['conn'])
               self.waiters.remove(w)
         self.cleanup_spool()
         self.cv.notify_all()

   #-------------------------------------------------------------------------------------
   # Scheduler , called every second and on every queue change , with the lock held

   def tick(self) :
      now = time.monotonic()
      h = self.holder
      if h :
         a = self.active
         contention = self.load_due() or any(a and w['path'] == a['path'] for w in self.waiters)
         due = self.stopping or (contention and now >= h['granted_at'] + h['t']*60)
         if not due :
            if h['warned_at'] is not None :
               h['warned_at'] = None
               send(h['conn'],'info','nobody is waiting any more , your access continues')
         elif h['warned_at'] is None :
            h['warned_at'] = now
            why = 'the queue manager is shutting down' if self.stopping else 'other users are waiting'
            send(h['conn'],'warn','%s , your board access ends in %d seconds' % (why,GRACE_SEC))
            log('Warned %s (%s)' % (h['user'],why))
         elif now >= h['warned_at'] + GRACE_SEC :
            send(h['conn'],'revoked','your board access time is over')
            self.event('Access of %s revoked after %s' % (h['user'],dur(now - h['granted_at'])))
            close_conn(h['conn'])
            self.holder = None

      if self.stopping :
         for w in self.waiters :
            send(w['conn'],'error','the queue manager is shutting down')
            close_conn(w['conn'])
         self.waiters = []
         if self.holder is None and self.loading is None :
            self.done = True
         return

      if self.holder or self.loading :
         return

      if self.load_due() :
         # Users waiting for the outgoing bitstream are queued behind the pending loads
         a = self.active
         if a and any(w['path'] == a['path'] for w in self.waiters) and not any(e['path'] == a['path'] for e in self.loads) :
            if a['snap'] :
               self.loads.append(dict(a, req_at=now))
            else :
               for w in [w for w in self.waiters if w['path'] == a['path']] :
                  send(w['conn'],'error','%s is no longer available to reload , run q55 access again' % a['path'])
                  close_conn(w['conn'])
                  self.waiters.remove(w)
         self.loading = self.loads.pop(0)
         for w in self.waiters :
            if w['path'] == self.loading['path'] :
               send(w['conn'],'info',self.loading_msg())
         threading.Thread(target=self.run_load,args=(self.loading,),daemon=True).start()
         return

      for w in self.waiters :
         if self.active and w['path'] == self.active['path'] :
            self.waiters.remove(w)
            w['granted_at'], w['warned_at'] = now, None
            self.holder = w
            self.event('Access granted to %s (%s , minimum %g min)' % (w['user'],w['path'],w['t']))
            send(w['conn'],'granted','board access granted for at least %g min , %s is loaded' % (w['t'],w['path']))
            break

   def scheduler(self) :
      with self.cv :
         while not self.done :
            self.tick()
            self.cv.wait(1.0)

   #-------------------------------------------------------------------------------------

   def release(self, user, admin) :
      # q55 release : the caller's turn is over. Ends the caller's access session at once (the manager
      # user ends anyone's) , cancels the caller's waiting access requests , and ends the allocation
      # window of the bitstream the caller loaded , unless other users wait for it. Called with the lock held.
      done = []
      h = self.holder
      if h and (h['user'] == user or admin) :
         send(h['conn'],'released','board access released by %s' % user)
         self.event('Access of %s released by %s after %s' % (h['user'],user,dur(time.monotonic() - h['granted_at'])))
         close_conn(h['conn'])
         self.holder = None
         done.append('released the board access of %s' % h['user'])
      mine = [w for w in self.waiters if w['user'] == user]
      for w in mine :
         send(w['conn'],'error','access request cancelled by q55 release')
         close_conn(w['conn'])
         self.waiters.remove(w)
      a = self.active
      if a and (a['user'] == user or admin) and self.window_left() > 0 and \
         not any(w['path'] == a['path'] and w['user'] != a['user'] for w in self.waiters) :
         a['t'] = (time.monotonic() - a['loaded_at'])/60   # shorten the window to now , keep the load time
         self.save_state()
         self.event('Allocation window of %s ended by %s' % (a['path'],user))
         done.append('ended the allocation window of %s (it stays loaded until another load is requested)' % a['path'])
      if mine :
         log('%s cancelled %d waiting access request(s)' % (user,len(mine)))
         done.append('cancelled %d waiting access request(s)' % len(mine))
      if not done :
         raise Refused('you hold no board access and have no waiting access requests')
      self.cv.notify_all()
      return 'q55 release: ' + ' , '.join(done)

   def status_text(self) :
      now = time.monotonic()
      L = ['U55C queue manager on %s , run by %s since %s' % (socket.gethostname(),pwd.getpwuid(os.getuid()).pw_name,
           datetime.fromtimestamp(self.started).strftime('%Y-%m-%d %H:%M'))]
      if self.stopping :
         L.append('SHUTTING DOWN')
      a = self.active
      if a :
         w = self.window_left()
         L.append('Active bitstream : %s' % a['path'])
         L.append('                   loaded by %s at %s , %s%s' % (a['user'],hhmm(a['loaded_at']),
                  'allocation window %s left' % dur(w) if w > 0 else 'allocation window over , stays loaded until another load is requested',
                  ' (restored from the previous manager run)' if a.get('restored') else ''))
      else :
         L.append('Active bitstream : none (or unknown)')
      if self.loading :
         L.append('Loading now      : %s (requested by %s)' % (self.loading['path'],self.loading['user']))
      h = self.holder
      if h :
         s = 'Access holder    : %s , since %s , minimum %g min' % (h['user'],hhmm(h['granted_at']),h['t'])
         if h['warned_at'] is not None :
            s += ' , revoked in %s' % dur(h['warned_at'] + GRACE_SEC - now)
         L.append(s)
      else :
         L.append('Access holder    : none')
      L.append('Access queue     : %s' % ('empty' if not self.waiters else ''))
      for i,w in enumerate(self.waiters) :
         L.append('   %d. %-10s %g min  waiting %s  %s' % (i+1,w['user'],w['t'],dur(now - w['req_at']),w['path']))
      L.append('Load queue       : %s' % ('empty' if not self.loads else ''))
      for i,e in enumerate(self.loads) :
         L.append('   %d. %-10s %g min  waiting %s  %s' % (i+1,e['user'],e['t'],dur(now - e['req_at']),e['path']))
      if self.events :
         L.append('Recent events    :')
         L += ['   ' + e for e in self.events]
      return '\n'.join(L)

   #-------------------------------------------------------------------------------------
   # Client connections

   def handle(self, conn) :
      try :
         pid, uid, gid = struct.unpack('3i',conn.getsockopt(socket.SOL_SOCKET,socket.SO_PEERCRED,struct.calcsize('3i')))
         try :
            user = pwd.getpwuid(uid).pw_name
         except KeyError :
            user = str(uid)
         data, fds, _, _ = socket.recv_fds(conn,65536,1)
         try :
            req = json.loads(data.decode())
            cmd = req['cmd']
            if cmd == 'status' :
               with self.cv :
                  send(conn,'end',self.status_text())
            elif cmd == 'shutdown' :
               if uid not in (0,os.getuid()) :
                  raise Refused('only %s can shut down the queue manager' % pwd.getpwuid(os.getuid()).pw_name)
               with self.cv :
                  self.stopping = True
                  self.event('Shutdown requested by %s' % user)
                  self.cv.notify_all()
               send(conn,'end','the queue manager is shutting down (after the current load and access session)')
            elif cmd == 'release' :
               with self.cv :
                  msg = self.release(user,uid in (0,os.getuid()))
               send(conn,'end',msg)
            elif cmd in ('prog','access') :
               if len(fds) != 1 :
                  raise Refused('no bitstream file received')
               msg = self.request_load(user,req['path'],fds[0],float(req['t']))
               if cmd == 'prog' :
                  send(conn,'end',msg)
               else :
                  self.access(conn,user,req,msg)
            else :
               raise Refused('unknown command %s' % cmd)
         except Refused as x :
            send(conn,'error',str(x))
         finally :
            for fd in fds : os.close(fd)
      except Exception as x :
         log('ERROR: client request failed: %r' % x)
         send(conn,'error','queue manager internal error: %r' % x)
      finally :
         conn.close()

   def access(self, conn, user, req, load_msg) :
      with self.cv :
         self.seq += 1
         w = dict(id=self.seq, user=user, path=req['path'], t=float(req['t']), conn=conn,
                  req_at=time.monotonic(), granted_at=None, warned_at=None)
         if self.stopping :
            raise Refused('the queue manager is shutting down')
         self.waiters.append(w)
         log('%s waits for access (%s , %g min)' % (user,w['path'],w['t']))
         send(conn,'info','%s\nWaiting for board access (%d in the access queue) , Ctrl-C to cancel'
              % (load_msg,len(self.waiters)))
         if self.loading and self.loading['path'] == w['path'] :
            send(conn,'info',self.loading_msg())   # the load started before this waiter was queued
         self.cv.notify_all()
      # The session lasts while the client keeps the connection open
      while True :
         try :
            if not conn.recv(4096) : break
         except OSError :
            break
      with self.cv :
         if self.holder is w :
            self.holder = None
            self.event('Access released by %s after %s' % (user,dur(time.monotonic() - w['granted_at'])))
         elif w in self.waiters :
            self.waiters.remove(w)
            log('%s cancelled the access request' % user)
         self.cv.notify_all()

#========================================================================================
# Server
#========================================================================================

def sudo_keepalive() :
   while True :
      time.sleep(SUDO_KEEPALIVE_SEC)
      if subprocess.call(['sudo','-n','-v'],stdin=subprocess.DEVNULL,stdout=subprocess.DEVNULL,stderr=subprocess.DEVNULL) != 0 :
         log('WARNING: sudo credential refresh failed , bitstream loads will fail')

def server() :
   me = pwd.getpwuid(os.getuid()).pw_name
   if me != MGR_USER :
      sys.exit('q55: the queue manager must run as %s (or set Q55_MGR_USER for every user)' % MGR_USER)
   os.makedirs(Q55_DIR,exist_ok=True)
   st = os.lstat(Q55_DIR)
   if st.st_uid != os.getuid() or not os.path.isdir(Q55_DIR) or os.path.islink(Q55_DIR) :
      sys.exit('q55: %s is not a directory owned by %s' % (Q55_DIR,me))
   os.chmod(Q55_DIR,0o755)
   os.makedirs(SPOOL_DIR,exist_ok=True)
   os.chmod(SPOOL_DIR,0o700)
   if os.path.exists(SOCK_PATH) :
      try :
         s = socket.socket(socket.AF_UNIX,socket.SOCK_STREAM)
         s.connect(SOCK_PATH)
         sys.exit('q55: a queue manager is already running')
      except OSError :
         os.remove(SOCK_PATH)
   if NEED_SUDO :
      print('The queue manager loads bitstreams with sudo , enter the %s password if asked' % me)
      if subprocess.call(['sudo','-v']) != 0 :
         sys.exit('q55: sudo failed')
      threading.Thread(target=sudo_keepalive,daemon=True).start()

   mgr = Mgr()
   for f in os.listdir(SPOOL_DIR) :          # leftovers of a previous run
      p = SPOOL_DIR + '/' + f
      if not (mgr.active and mgr.active['snap'] == p) : os.remove(p)

   srv = socket.socket(socket.AF_UNIX,socket.SOCK_STREAM)
   srv.bind(SOCK_PATH)
   os.chmod(SOCK_PATH,0o666)
   srv.listen(16)
   srv.settimeout(1.0)
   threading.Thread(target=mgr.scheduler,daemon=True).start()
   log('U55C queue manager started (pid %d , socket %s , load command %s)' % (os.getpid(),SOCK_PATH,PROG_CMD))

   while not mgr.done :
      try :
         conn, _ = srv.accept()
         conn.settimeout(None)
         threading.Thread(target=mgr.handle,args=(conn,),daemon=True).start()
      except socket.timeout :
         pass
      except KeyboardInterrupt :
         with mgr.cv :
            if mgr.stopping :
               log('Second Ctrl-C , exiting now')
               break
            mgr.stopping = True
            mgr.event('Shutdown requested (Ctrl-C) , Ctrl-C again to exit now')
            mgr.cv.notify_all()
   srv.close()
   os.remove(SOCK_PATH)
   log('U55C queue manager stopped')

#========================================================================================
# Client
#========================================================================================

def connect() :
   try :
      owner = pwd.getpwuid(os.stat(Q55_DIR).st_uid).pw_name
      if owner != MGR_USER :
         sys.exit('q55: %s is owned by %s , not by the queue manager user %s' % (Q55_DIR,owner,MGR_USER))
      s = socket.socket(socket.AF_UNIX,socket.SOCK_STREAM)
      s.connect(SOCK_PATH)
      return s
   except (OSError,KeyError) :
      sys.exit('q55: the U55C queue manager is not running (ask the %s account to run: q55 server)' % MGR_USER)

def request(req, bit=None) :
   s = connect()
   fds = []
   if bit is not None :
      if not os.path.isfile(bit) :
         sys.exit('q55: bitstream %s not found' % bit)
      req['path'] = os.path.realpath(bit)
      fds = [os.open(bit,os.O_RDONLY)]
   socket.send_fds(s,[(json.dumps(req) + '\n').encode()],fds)
   for fd in fds : os.close(fd)
   return s, (json.loads(line) for line in s.makefile('r',encoding='utf-8'))

def simple_cmd(req, bit=None) :
   s, msgs = request(req,bit)
   for m in msgs :
      print(m['msg'])
      sys.exit(1 if m['type'] == 'error' else 0)
   sys.exit('q55: no answer from the queue manager')

def access_cmd(bit, t, cmd) :
   s, msgs = request({'cmd':'access','t':t},bit)
   try :
      for m in msgs :
         print(m['msg'],flush=True)
         if m['type'] == 'error' : sys.exit(1)
         if m['type'] == 'granted' : break
      else :
         sys.exit('q55: the queue manager closed the connection')
   except KeyboardInterrupt :
      sys.exit('\nq55: access request cancelled')

   # Run the session in its own process group , in the terminal foreground , so that it can be revoked as a whole
   tty = sys.stdin.isatty()
   def child_setup() :
      os.setpgid(0,0)
      if tty :
         signal.signal(signal.SIGTTOU,signal.SIG_IGN)
         os.tcsetpgrp(0,os.getpgrp())
         signal.signal(signal.SIGTTOU,signal.SIG_DFL)
   env = dict(os.environ, Q55_ACCESS='1', Q55_BITSTREAM=os.path.realpath(bit))
   rcfile = None
   if not cmd :
      sh = os.environ.get('SHELL','/bin/bash')
      cmd = [sh,'-i']
      if os.path.basename(sh) == 'bash' :
         # Mark the access shell prompt with (q55) , after the user's own bashrc
         with tempfile.NamedTemporaryFile('w',prefix='q55_',suffix='.bashrc',delete=False) as f :
            f.write('[ -f /etc/bash.bashrc ] && . /etc/bash.bashrc\n'
                    '[ -f ~/.bashrc ] && . ~/.bashrc\n'
                    'PS1="(q55) $PS1"\n')
         rcfile = f.name
         cmd = [sh,'--rcfile',rcfile,'-i']
      print('*** q55: you now hold the U55C board. This is a new shell (prompt marked (q55)) in the same directory.\n'
            '*** Run your work here , then type exit (or q55 release) to release the board for the next user.',flush=True)
   p = subprocess.Popen(cmd,env=env,preexec_fn=child_setup)
   ended = [None]   # 'revoked' or 'released' when the manager ends the session

   def watch() :
      for m in msgs :
         print('\n*** q55: %s ***' % m['msg'],file=sys.stderr,flush=True)
         if m['type'] in ('revoked','released') :
            ended[0] = m['type']
            for sig,wait in ((signal.SIGHUP,5),(signal.SIGKILL,0)) :
               try :
                  os.killpg(p.pid,sig)
               except ProcessLookupError :
                  return
               end = time.monotonic() + wait
               while p.poll() is None and time.monotonic() < end : time.sleep(0.1)
            return

   threading.Thread(target=watch,daemon=True).start()
   signal.signal(signal.SIGINT,signal.SIG_IGN)
   rc = p.wait()
   if rcfile :
      os.remove(rcfile)
   if tty :
      signal.signal(signal.SIGTTOU,signal.SIG_IGN)
      os.tcsetpgrp(0,os.getpgrp())
   s.close()
   print('q55: board access %s' % (ended[0] or 'released'))
   sys.exit({'revoked':2,'released':0}.get(ended[0],rc))

#========================================================================================

def main() :
   argv = sys.argv[1:]
   cmd = []
   if '--' in argv :
      i = argv.index('--')
      argv, cmd = argv[:i], argv[i+1:]
   # The manager commands are listed in the help only for the manager user (and root)
   mgr = os.getuid() == 0 or pwd.getpwuid(os.getuid()).pw_name == MGR_USER
   ops = ['prog','access','status','release'] + (['server','shutdown'] if mgr else [])
   ap = argparse.ArgumentParser(prog='q55',description='Alveo U55C board access queue manager')
   sub = ap.add_subparsers(dest='op',required=True,metavar='{%s}' % ','.join(ops))
   def mgr_help(hlp) :
      return dict(help=hlp) if mgr else {}   # add_parser without help= is not listed
   for op,hlp in (('prog','queue a bitstream load request'),
                  ('access','queue an access request : waits for exclusive board access , loads the bitstream '
                            'if needed , then runs <cmd> (after --) or a shell')) :
      p = sub.add_parser(op,help=hlp)
      p.add_argument('bitstream')
      p.add_argument('-t',metavar='<minutes>',type=float,default=DEF_MIN,
                     help='allocation time once loaded / granted (default %d min)' % DEF_MIN)
   sub.add_parser('status',help='queue status')
   sub.add_parser('release',help='end your turn now (even before -t is over) : release your board access , '
                                 'end the allocation window of your bitstream and cancel your waiting access requests')
   sub.add_parser('server',**mgr_help('run the queue manager (%s account)' % MGR_USER))
   sub.add_parser('shutdown',**mgr_help('graceful queue manager shutdown (%s account)' % MGR_USER))
   args = ap.parse_args(argv)

   if cmd and args.op != 'access' :
      ap.error('-- <cmd> is only valid with access')
   if args.op in ('prog','access') and args.t <= 0 :
      ap.error('-t must be positive')

   if args.op == 'server'     : server()
   elif args.op == 'prog'     : simple_cmd({'cmd':'prog','t':args.t},args.bitstream)
   elif args.op == 'access'   : access_cmd(args.bitstream,args.t,cmd)
   else :
      if args.op == 'release' :
         # Run from inside the access session , leave its process group , which is about to be killed
         try :
            os.setpgid(0,0)
         except OSError :
            pass
      simple_cmd({'cmd':args.op})

if __name__ == '__main__' :
   main()
