from pathlib import Path
import argparse,hashlib,json,os,sys,time
from audit_fence import install
AUDIT_HITS, _ = install(Path(__file__).resolve().parent, child=True)
scripts=Path(__file__).parent/'scripts'
sys.path.insert(0,str(scripts))
import _cli
parser=_cli.build_parser('fixture',1)
a=parser.parse_args(sys.argv[1:])
path=Path(a.prompt_file);raw=path.read_bytes()
# Execute the frozen source's exact prompt-file intake AST, without importing backend code.
import ast
source=(Path(__file__).parent/'scripts'/'run_subagent.py').read_text(encoding='utf-8')
main=next(node for node in ast.parse(source).body if isinstance(node,ast.FunctionDef) and node.name=='main')
candidates=[node for node in main.body if isinstance(node,ast.If) and ast.unparse(node.test)=='args.prompt_file is not None']
assert len(candidates)==1
function=ast.FunctionDef(name='intake',args=ast.arguments(posonlyargs=[],args=[ast.arg(arg='args')],kwonlyargs=[],kw_defaults=[],defaults=[]),body=[candidates[0],ast.Return(value=ast.Attribute(value=ast.Name(id='args',ctx=ast.Load()),attr='prompt',ctx=ast.Load()))],decorator_list=[])
unit=ast.fix_missing_locations(ast.Module(body=[function],type_ignores=[]))
def refusal(*unused):raise ValueError('source intake refused')
namespace={'_die':refusal}
exec(compile(unit,'source-qualified-prompt-file-intake','exec'),namespace)
decoded=namespace['intake'](a)
out=Path(a.out)
observation={'bytes':len(raw),'sha256':hashlib.sha256(raw).hexdigest(),'decoded_bytes':len(decoded.encode()),'decoded_sha256':hashlib.sha256(decoded.encode()).hexdigest(),'file_exists_during_child':path.exists(),'chair_clamped':a.max_permission=='read-only','require_tools':a.require_tools,'source':path.name}
out.with_suffix('.observation.json').write_text(json.dumps(observation))
assert AUDIT_HITS == []
if os.environ.get('SYNTHETIC_DISPATCH_FAIL')=='1':raise SystemExit(7)
result=('DECISION: X' if a.agent=='chair' else a.agent+' original'+('\nRANKING: A, B' if '-r2-' in out.stem else ''))
env={'status':'success','result':result,'report':{'summary':result},'attempts':1,'attempt_id':out.stem+'-attempt','provider_contacted':False,'uncertain_spend':False}
out.write_text(json.dumps(env),encoding='utf-8')
print('SYNTHETIC STDOUT BANNER: authoritative envelope is in --out')

assert AUDIT_HITS == []


