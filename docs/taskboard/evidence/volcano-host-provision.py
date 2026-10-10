from pathlib import Path
import json,yaml,os
root=Path('/home/leewanlong/work_leewanlong/code-projects/artifacts/taskboard-mvp-demo')
os.environ['JIUWENSWARM_DATA_DIR']=str(root/'.taskboard-demo')
os.environ['JIUWENSWARM_CONFIG_DIR']=str(root/'.taskboard-demo/config')
from jiuwenswarm.common.utils import get_agent_root_dir
from jiuwenswarm.governance.host_identity import local_instance_identity
from jiuwenswarm.governance.model_credentials import ModelCredentialBinding
from jiuwenswarm.governance.resources import ResourceDefinition
from jiuwenswarm.server.runtime.session.project_access import ProjectAccessStore
identity=local_instance_identity('127.0.0.1',get_agent_root_dir())
cfg=yaml.safe_load((root/'.taskboard-demo/config/config.yaml').read_text())
binding=ModelCredentialBinding.from_config(cfg['models']['defaults'][0]['model_client_config'])
store=ProjectAccessStore();pid='proj_7f6f3ee7'
with store._locked():
 record=store._load()['projects'][pid]
 assert record['owner_id']==identity.actor_id
 assert store.authorize(pid,identity.actor_id,'admin').allowed
 state=store._resource_state(record)
 revision=store.register_resource(pid,ResourceDefinition('taskboard-volcano-model','credential',binding.reference),owner_subject_id=identity.subject_id,actions=('use',),expected_revision=state['revision'],delegable=False)
record={'project_id':pid,'resource_id':'taskboard-volcano-model','kind':'credential','reference':binding.reference,'actions':['use'],'subject':'current local installation owner','resource_revision':revision,'delegable':False,'interface':'ProjectAccessStore.register_resource (host provisioning)','model':'glm-5.2','api_base':binding.api_base,'authorization':'user requested existing Volcano configuration; exact owner and project admin checked; no wire registration or policy bypass','native_first_attempt':'denied 181006 before model credential was provisioned','native_first_session':'web_1a1267fff45_4a5dee0a139c'}
(root/'docs/taskboard/evidence/volcano-resource.json').write_text(json.dumps(record,ensure_ascii=False,indent=2))
print(json.dumps(record,ensure_ascii=False))
