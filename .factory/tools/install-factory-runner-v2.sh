#!/bin/sh
# Transactional authenticated installer for the generic runner authority.
set -eu
umask 077
fail(){ echo "runner-installer: $*" >&2;exit 1; }
usage(){ fail 'usage: install-factory-runner-v2.sh install|verify --source-root DIR --install-manifest FILE --policy FILE --transport-manifest FILE --ssh-launcher-manifest FILE [--key account=public-key]...'; }
[ "$(/usr/bin/id -u)" = 0 ] || fail 'root required'
verb=${1-};shift || true
SOURCE=''; MANIFEST=''; POLICY=''; TRANSPORT=''; LAUNCHER=''; KEYS=''
while [ "$#" -gt 0 ];do case "$1" in --source-root) SOURCE=$2;shift 2;;--install-manifest) MANIFEST=$2;shift 2;;--policy) POLICY=$2;shift 2;;--transport-manifest) TRANSPORT=$2;shift 2;;--ssh-launcher-manifest) LAUNCHER=$2;shift 2;;--key) KEYS="${KEYS}${KEYS:+
}$2";shift 2;;*)usage;;esac;done
[ "$verb" = install ] || [ "$verb" = verify ] || usage
[ -n "$SOURCE" ]&&[ -n "$MANIFEST" ]&&[ -n "$POLICY" ]&&[ -n "$TRANSPORT" ]&&[ -n "$LAUNCHER" ]||usage
[ "$verb" != install ] || [ "${FACTORY_RUNNER_AUTHENTICATED_BOOTSTRAP-}" = 1 ] || fail 'installation must enter through authenticated root bootstrap'
/usr/bin/python3 -I - "$verb" "$SOURCE" "$MANIFEST" "$POLICY" "$TRANSPORT" "$LAUNCHER" "$KEYS" <<'PY'
import hashlib,json,os,pathlib,pwd,re,shutil,stat,sys,tempfile
verb,source,manifest,policy_path,transport_path,launcher_path,keyraw=sys.argv[1:];source=pathlib.Path(source);state=pathlib.Path('/var/lib/factory-runner-installer');generations=pathlib.Path('/opt/factory-runner/generations');current=pathlib.Path('/opt/factory-runner/current')
def die(s):raise SystemExit('runner-installer: '+s)
def read(path,maximum=128*1024*1024,root=False):
 p=pathlib.Path(path)
 if not p.is_absolute():die('input path must be absolute')
 cur=pathlib.Path('/')
 for part in p.parts[1:]:
  cur/=part;i=os.lstat(cur);leaf=cur==p
  if stat.S_ISLNK(i.st_mode) or (root and i.st_uid!=0) or i.st_mode&0o022 or (not leaf and not stat.S_ISDIR(i.st_mode)):die('unsafe input ancestry')
 fd=os.open(p,os.O_RDONLY|os.O_NOFOLLOW|os.O_CLOEXEC);i=os.fstat(fd)
 try:
  if not stat.S_ISREG(i.st_mode) or i.st_nlink!=1 or i.st_size>maximum:die('unsafe input inode')
  return os.read(fd,i.st_size+1)
 finally:os.close(fd)
def canonical(path):return json.loads(read(path))
manifest_raw=read(manifest,root=True);policy_raw=read(policy_path,root=True);transport_raw=read(transport_path,root=True);launcher_raw=read(launcher_path,root=True)
m=json.loads(manifest_raw);policy=json.loads(policy_raw);transport=json.loads(transport_raw);launcher=json.loads(launcher_raw)
if m.get('schema')!='factory-runner-install-manifest/v2':die('install manifest schema invalid')
if policy.get('schema')!='factory-runner-policy/v3' or set(policy)!={'schema','namespace','classes'} or not 1<=len(policy['classes'])<=64:die('policy schema/classes invalid')
if transport.get('schema')!='factory-runner-transport/v1' or set(transport)!={'schema','classes'}:die('transport manifest invalid')
if launcher.get('schema')!='factory-ssh-launcher/v1' or set(launcher)!={'schema','path','sha256','device','inode'}:die('launcher enrollment invalid')
identifier=re.compile(r'^[a-z][a-z0-9-]{0,62}$')
classes={c.get('name'):c for c in policy['classes']};accounts={c.get('account') for c in policy['classes']}
if (None in classes or len(classes)!=len(policy['classes']) or None in accounts or len(accounts)!=len(classes)
    or any(not isinstance(x,str) or not identifier.fullmatch(x) for x in (*classes,*accounts))):die('class/account mappings must be unique strict identifiers')
if set(transport['classes'])!=set(classes):die('transport classes differ from policy')
keys={}
for line in keyraw.splitlines():
 if not line:continue
 account,sep,path=line.partition('=')
 if not sep or account in keys:die('key mapping invalid or duplicate')
 keys[account]=path
if set(keys)!=accounts or any(not identifier.fullmatch(account) for account in keys):die('exactly one public key is required for every configured account')
for keypath in keys.values(): read(keypath,65536,root=True)

required={'.factory/tools/factory-runner-broker.py','.factory/tools/factory-runner-signer.py','.factory/tools/factory-runner-server.py','.factory/tools/factory_runner_policy.py','.factory/tools/factory_runner_authority.py','.factory/tools/factory_runner_artifacts.py'}
if not required.issubset(m.get('files',{})):die('signed install closure incomplete')
# Invoke the generic policy authority from bytes authenticated by the install
# manifest before creating or replacing a single installation path.
policy_desc=m['files']['.factory/tools/factory_runner_policy.py'];policy_code=read(source/'.factory/tools/factory_runner_policy.py',policy_desc['size']+1,root=True)
if len(policy_code)!=policy_desc['size'] or hashlib.sha256(policy_code).hexdigest()!=policy_desc['sha256']:die('generic policy validator differs from authenticated manifest')
policy_namespace={'__name__':'factory_runner_policy_install_validation'}
try:exec(compile(policy_code,'<authenticated-factory-runner-policy>','exec'),policy_namespace);policy=policy_namespace['validate_policy'](policy)
except Exception as e:die('generic policy validator rejected policy: '+str(e))
generation=m['source_merkle_sha256'];target=generations/generation

def verify_file(path,desc):
 raw=read(path,len(read(path))+1);i=os.stat(path)
 if len(raw)!=desc['size'] or hashlib.sha256(raw).hexdigest()!=desc['sha256'] or stat.S_IMODE(i.st_mode)!=desc['mode']:die('installed file differs from signed tree')
def verify_generation():
 if not target.is_dir() or target.is_symlink():die('generation absent or unsafe')
 for rel,desc in m['files'].items():verify_file(target/'source'/rel,desc)
 if not current.is_symlink() or current.resolve()!=target:die('current generation pointer is invalid')
 if json.loads(read('/etc/factory-runner/runner-policy.json'))!=policy:die('installed policy differs')
 for name,c in classes.items():
  account=c['account'];home=pathlib.Path(pwd.getpwnam(account).pw_dir);ak=home/'.ssh/authorized_keys';raw=read(ak,65536).decode()
  key=read(keys[account],65536,root=True).decode().strip();forced=f'restrict,command="/usr/local/libexec/factory-runner-server" {key}'
  if raw!=forced+'\n':die('authorized_keys does not contain exact restrictive ForcedCommand')
 if json.loads(read('/etc/factory-runner/client/ssh-launcher.json'))!=launcher:die('launcher manifest differs')
if verb=='verify':verify_generation();print('runner-installer: installed generation verified');raise SystemExit(0)
# Recover interrupted cutover before staging. Journal is root-only and names old/new.
state.mkdir(mode=0o700,parents=True,exist_ok=True);generations.mkdir(mode=0o755,parents=True,exist_ok=True)
journal=state/'transaction.json'
if journal.exists():
 j=json.loads(read(journal));old=j.get('old')
 for item in reversed(j.get('replaced',[])):
  dest=pathlib.Path(item['dest']);backup=pathlib.Path(item['backup']) if item['backup'] else None
  if dest.exists() or dest.is_symlink():
   if dest.is_dir() and not dest.is_symlink():shutil.rmtree(dest)
   else:dest.unlink()
  if backup is not None and (backup.exists() or backup.is_symlink()):os.replace(backup,dest)
 if old and pathlib.Path(old).is_dir() and not current.exists():
  tmp=current.with_name('.current-recover');tmp.unlink(missing_ok=True);tmp.symlink_to(old);os.replace(tmp,current)
 journal.unlink()
if target.exists():
 # Idempotent only for exact complete generation.
 verify_generation();print('runner-installer: generation already installed');raise SystemExit(0)
stage=pathlib.Path(tempfile.mkdtemp(prefix='.generation-',dir=generations));old=str(current.resolve()) if current.is_symlink() else None
backupdir=pathlib.Path(tempfile.mkdtemp(prefix='transaction-backup-',dir=state));replaced=[]
def replace_transactional(temp,dest):
 dest=pathlib.Path(dest);backup=backupdir/str(len(replaced))
 if dest.exists() or dest.is_symlink():os.replace(dest,backup);replaced.append((dest,backup))
 else:replaced.append((dest,None))
 os.replace(temp,dest)
 data={'old':old,'new':str(target),'replaced':[{'dest':str(d),'backup':str(b) if b else None} for d,b in replaced]}
 jt=state/'.transaction.next';jt.unlink(missing_ok=True);fd=os.open(jt,os.O_WRONLY|os.O_CREAT|os.O_EXCL|os.O_NOFOLLOW,0o600);os.write(fd,(json.dumps(data,sort_keys=True)+'\n').encode());os.fsync(fd);os.close(fd);os.replace(jt,journal)
try:
 (stage/'source').mkdir(mode=0o700)
 for rel,desc in m['files'].items():
  body=read(source/rel,desc['size']+1,root=True)
  if len(body)!=desc['size'] or hashlib.sha256(body).hexdigest()!=desc['sha256']:die('source differs from authenticated manifest')
  dest=stage/'source'/rel;dest.parent.mkdir(mode=0o700,parents=True,exist_ok=True);fd=os.open(dest,os.O_WRONLY|os.O_CREAT|os.O_EXCL|os.O_NOFOLLOW,desc['mode']);os.write(fd,body);os.fsync(fd);os.close(fd);os.chmod(dest,desc['mode'])
 os.rename(stage,target);stage=None
 jf=os.open(journal,os.O_WRONLY|os.O_CREAT|os.O_EXCL|os.O_NOFOLLOW,0o600);os.write(jf,(json.dumps({'old':old,'new':str(target),'replaced':[]},sort_keys=True)+'\n').encode());os.fsync(jf);os.close(jf)
 # Install compatibility executables from immutable generation by copy; verify closes the loop.
 lib=pathlib.Path('/usr/local/libexec');lib.mkdir(parents=True,exist_ok=True)
 for srcname,destname in [('factory-runner-server.py','factory-runner-server'),('factory-runner-broker.py','factory-runner-broker'),('factory-runner-signer.py','factory-runner-signer')]:
  src=target/'source/.factory/tools'/srcname;tmp=lib/('.'+destname+'.new');tmp.unlink(missing_ok=True);shutil.copyfile(src,tmp);os.chmod(tmp,0o755);replace_transactional(tmp,lib/destname)
 bundle=lib/'factory-runner-v2.bundle';bt=lib/'.factory-runner-v2.bundle.new';shutil.rmtree(bt,ignore_errors=True);bt.mkdir(mode=0o755)
 for n in ('factory_runner_policy.py','factory_runner_authority.py','factory_runner_artifacts.py'):shutil.copyfile(target/'source/.factory/tools'/n,bt/n);os.chmod(bt/n,0o644)
 replace_transactional(bt,bundle)
 etc=pathlib.Path('/etc/factory-runner');(etc/'client').mkdir(parents=True,exist_ok=True);(etc/'principals').mkdir(parents=True,exist_ok=True)
 for path,body,mode in [(etc/'runner-policy.json',policy_raw,0o400),(etc/'transport-manifest.json',transport_raw,0o400),(etc/'client/ssh-launcher.json',launcher_raw,0o400)]:
  tmp=path.with_name('.'+path.name+'.new');tmp.write_bytes(body);os.chmod(tmp,mode);replace_transactional(tmp,path)
 sudo=pathlib.Path('/etc/sudoers.d/factory-runner-broker');sudo_body=('\n'.join(f'{c["account"]} ALL=(root) NOPASSWD: /usr/local/libexec/factory-runner-broker' for c in policy['classes'])+'\n').encode()
 sudo_stage=pathlib.Path(tempfile.mkstemp(prefix='factory-runner-sudoers-',dir='/etc/sudoers.d')[1]);sudo_stage.write_bytes(sudo_body);os.chmod(sudo_stage,0o440)
 checked=os.spawnv(os.P_WAIT,'/usr/sbin/visudo',['visudo','-cf',str(sudo_stage)])
 if checked!=0: sudo_stage.unlink(missing_ok=True);die('visudo rejected staged policy')
 replace_transactional(sudo_stage,sudo)
 for c in policy['classes']:
  account=c['account'];home=pathlib.Path(pwd.getpwnam(account).pw_dir);ssh=home/'.ssh'
  if ssh.exists() and (ssh.is_symlink() or not ssh.is_dir()):die('unsafe .ssh path')
  ssh.mkdir(mode=0o700,exist_ok=True);os.chown(ssh,c['uid'],pwd.getpwnam(account).pw_gid);os.chmod(ssh,0o700)
  ak=ssh/'authorized_keys'
  if ak.exists() and (ak.is_symlink() or not ak.is_file()):die('unsafe authorized_keys path')
  key=read(keys[account],65536,root=True).decode().strip();line=f'restrict,command="/usr/local/libexec/factory-runner-server" {key}\n';aktmp=ssh/'.authorized_keys.new';aktmp.unlink(missing_ok=True);fd=os.open(aktmp,os.O_WRONLY|os.O_CREAT|os.O_EXCL|os.O_NOFOLLOW,0o600);os.write(fd,line.encode());os.fsync(fd);os.fchown(fd,c['uid'],pwd.getpwnam(account).pw_gid);os.close(fd);replace_transactional(aktmp,ak)
 tmp=current.with_name('.current-new');tmp.unlink(missing_ok=True);tmp.symlink_to(target);replace_transactional(tmp,current);verify_generation();journal.unlink();shutil.rmtree(backupdir);print('runner-installer: transactional generation installed')
except BaseException:
 if stage:shutil.rmtree(stage,ignore_errors=True)
 for dest,backup in reversed(replaced):
  if dest.exists() or dest.is_symlink():
   if dest.is_dir() and not dest.is_symlink():shutil.rmtree(dest)
   else:dest.unlink()
  if backup is not None:os.replace(backup,dest)
 shutil.rmtree(backupdir,ignore_errors=True)
 if target.exists():shutil.rmtree(target,ignore_errors=True)
 raise
PY
