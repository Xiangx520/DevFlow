"""Disposable UI fixture. No worker, model, GitHub or Feishu calls. Token: ui-test-token.
Run: .venv/Scripts/python.exe scripts/frontend_fixture.py (API on 8101).
"""
import os
import tempfile
import time
from pathlib import Path


def main():
    import uvicorn
    with tempfile.TemporaryDirectory(prefix='devflow-ui-') as directory:
        os.chdir(directory)  # Never load the project's real .env.
        os.environ.update(DEVFLOW_DATABASE_URL='sqlite:///'+str(Path(directory)/'fixture.db'),
                          DEVFLOW_API_TOKEN='ui-test-token', DEVFLOW_GITHUB_TOKEN='', DEVFLOW_LLM_API_KEY='',
                          DEVFLOW_FEISHU_WEBHOOK='', DEVFLOW_FEISHU_APP_SECRET='')
        from devflow.db import engine, transaction
        from devflow.models import Base, Task, Artifact, Conversation, Message, Finding, Decision
        from devflow import core, router_state
        Base.metadata.create_all(engine())
        with transaction() as db:
            project=core.new_project(db,'example/devflow-sandbox',{'allowed_actors':['example'],'mode':'observe'},
                {'setup_commands':['pip install pytest'],'verify_commands':['python -m pytest -q'],'notify_feishu':False})
            router_state.ensure(db, project.id).summary='隔离 UI 验证环境：先完成登录修复，再推进依赖它的接口集成；只对必要决策进行人工交接。'
            first=core.create_task(db,project.id,'修复登录态失效后的重试逻辑','保留用户输入，验证登录重试不会重复提交。',['新增回归测试','验证当前提交'],scope='implement',priority=5)
            second=core.create_task(db,project.id,'集成用户资料接口','接口集成依赖登录修复。',['完成接口验证'],scope='implement',priority=2)
            core.add_dependency(db,second.id,first.id)
            core.ask(db,first,'登录失效后应保留多久的草稿？','当前标签页内保留，主动退出时清除。','影响交互与数据保留行为')
            ready=core.create_task(db,project.id,'为开发仓库补齐 CI 测试','添加自动测试并提供当前提交的结果。',['测试成功'],scope='implement')
            ready.status='ready_for_review';ready.pr_number=12;ready.head_sha='a'*40
            db.add(Artifact(task_id=ready.id,kind='verification',sha=ready.head_sha,verified=True,payload={'command':'python -m pytest -q','result':'3 passed','exit_code':0}))
            db.add(Finding(task_id=first.id,evidence_key='fixture-drift',kind='scope_drift',evidence={'summary':'执行者准备修改非目标模块'},recommendation='缩小修改范围，先验证登录修复。'))
            conv=Conversation(project_id=project.id);db.add(conv);db.flush()
            db.add(Message(conversation_id=conv.id,role='user',content='先解决登录问题，再进行接口集成。',created_at=time.time()-60))
            db.add(Message(conversation_id=conv.id,role='assistant',content='已记录优先级和依赖。登录任务等待草稿保留策略的决定；接口集成会在前置任务完成后继续。'))
            db.add(Decision(project_id=project.id,trigger_id='fixture',action={'name':'dependency','arguments':{'task_id':second.id,'prerequisite_id':first.id}},result={'applied':True},rationale='接口集成依赖登录能力，保守串行避免重叠修改。'))
        print('Disposable UI API: http://127.0.0.1:8101 (no worker)',flush=True)
        try:
            uvicorn.run('devflow.api:app',host='127.0.0.1',port=8101)
        finally:
            engine().dispose()

if __name__=='__main__':main()
