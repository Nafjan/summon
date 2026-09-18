"""Owned byte/argv consumer; no vendor decoder or provider is invoked."""
from pathlib import Path
import hashlib, json, os, sys
from audit_fence import install
HITS, _ = install(Path(__file__).resolve().parent, child=True)
mode, output = sys.argv[1:3]
out=Path(output)
if mode=='acp':
 raw=sys.stdin.buffer.readline()
 result={'bytes':len(raw),'sha256':hashlib.sha256(raw).hexdigest()}
elif mode=='gemini':
 raw=Path(os.environ['GEMINI_SYSTEM_MD']).read_bytes()
 decoded=raw.decode('utf-8')
 result={'bytes':len(raw),'sha256':hashlib.sha256(raw).hexdigest(),'decoded_sha256':hashlib.sha256(decoded.encode('utf-8')).hexdigest(),'argv':sys.argv[3:]}
else:
 result={'argv':sys.argv[3:],'env':dict(os.environ)}
assert HITS==[]
out.write_text(json.dumps(result),encoding='utf-8')
if mode=='gemini':
 print(json.dumps({'type':'init','session_id':'synthetic-session'}))
 print(json.dumps({'type':'message','role':'assistant','content':'synthetic complete'}))
 print(json.dumps({'type':'result','status':'success'}))
elif mode=='ark':print(json.dumps({'result':'synthetic complete'}))
