import { request, consumeEvents, mergeEvents, escapeHTML as e } from './api.js';
import * as ui from './ui.js';
const $ = (selector, root=document) => root.querySelector(selector);
const state = { token:sessionStorage.getItem('devflow-token')||'', projects:[],project:null,tasks:[],attentions:[],events:[],decisions:[],conversations:[],messages:[],conversationId:'',health:null,watchdog:null,watchdogAnswer:'',page:'overview',filter:'all',search:'',generation:0 };
let eventController, refreshTimer, refreshInFlight=false, refreshAgain=false, modalTask=null, modalData=null, modalTab='summary', activeModal='', lastFocus=null;
const submissions=new Set();
const pages = {overview:ui.overview,tasks:ui.tasksPage,attention:ui.attentionPage,conversation:ui.chatPage,activity:ui.activityPage,settings:ui.settingsPage};
const api=(path,options={})=>request(path,options,state.token);
function toast(message,error=false) { const el=document.createElement('div'); el.className='toast'+(error?' error':'');el.setAttribute('role',error?'alert':'status');el.textContent=message;$('#toast-root').append(el);setTimeout(()=>el.remove(),error?9000:4000); }
function errorMessage(err) { return err.status===401?'登录已失效，请重新输入 API Token。':err.message==='Failed to fetch'?'无法连接后端，请检查服务是否启动。':err.message; }
function handleError(err) { if(err.name==='AbortError')return; if(err.status===401){logout();$('#login-form .form-error').textContent=errorMessage(err);}else toast(errorMessage(err),true); }
function connection(text,online) { const el=$('#connection');if(el){el.className='connection '+(online?'connected':'disconnected');el.innerHTML=ui.icon('activity')+e(text);} }
function remember(key,value){sessionStorage.setItem(key,value);}
function logout(){eventController?.abort();clearTimeout(refreshTimer);state.generation++;state.token='';state.project=null;sessionStorage.removeItem('devflow-token');closeModal();$('#app').innerHTML=ui.login();}
function pageFromHash(){const route=location.hash.slice(1).split('?')[0];return pages[route]?route:'overview';}
function capture(root) {
 const focused=document.activeElement;
 return [...root.querySelectorAll('input,textarea,select')].filter(el=>el.id||el.name).map(el=>({key:el.id?`#${el.id}`:`[name="${el.name}"]`,form:el.form?.id||null,value:el.value,checked:el.checked,focus:el===focused,start:el.selectionStart,end:el.selectionEnd}));
}
function restore(root,values){for(const item of values){const el=$(item.key,item.form?$('#'+item.form,root)||root:root);if(!el)continue;el.value=item.value;if(['checkbox','radio'].includes(el.type))el.checked=item.checked;if(item.focus){el.focus({preventScroll:true});if(item.start!==null)el.setSelectionRange?.(item.start,item.end);}}}
function render(preserve=true){
 const content=$('#content');if(!content)return;
 const values=preserve?capture(content):[];
 const forms=preserve?[...content.querySelectorAll('form')].map(f=>({id:f.id,attention:f.dataset.attention,data:{...f.dataset}})):[];
 const oldMessages=$('#messages'),scroll=oldMessages?.scrollTop,wasBottom=oldMessages?oldMessages.scrollHeight-oldMessages.scrollTop-oldMessages.clientHeight<80:true;
 if(!state.project){content.innerHTML=`${ui.empty('接入你的第一个 GitHub 仓库','配置仓库与执行规则后，就可以和项目助手一起推进开发。',ui.button('new-project','接入仓库','primary','plus'),'git')}`;return;}
 content.innerHTML=pages[state.page](state);
 restore(content,values);
 for(const old of forms){const f=old.id?document.getElementById(old.id):content.querySelector(`[data-attention="${old.attention}"]`);if(f){Object.assign(f.dataset,old.data);if(submissions.has(old.id||old.attention))f.querySelector('[type="submit"]')?.setAttribute('disabled','');}}
 if($('#messages'))$('#messages').scrollTop=wasBottom?$('#messages').scrollHeight:scroll;
 document.querySelectorAll('.nav-item').forEach(el=>el.classList.toggle('active',el.hash==='#'+state.page));
 const pending=state.attentions.length+state.tasks.filter(t=>t.status==='ready_for_review').length;
 const nav=$('.nav-item[href="#attention"]');if(nav){$('.nav-count',nav)?.remove();if(pending)nav.insertAdjacentHTML('beforeend',`<b class="nav-count">${pending}</b>`);}
 document.title=`${ui.navItems.find(x=>x[0]===state.page)?.[2]} · DevFlow`;
}
async function loadConversation(id,older=false){
 if(!id){state.messages=[];return;}
 const generation=state.generation;
 const before=older&&state.messages[0]?`&before_id=${encodeURIComponent(state.messages[0].id)}`:'';
 const rows=await api(`/conversations/${id}/messages?limit=100${before}`);
 if(generation!==state.generation||state.conversationId!==id)return;
 if(older){state.messages=[...rows,...state.messages];if(!rows.length)toast('没有更早的消息了。');}
 else {
  const previous=state.messageConversation===id?state.messages:[];
  state.messages=previous.length>100?[...new Map([...previous,...rows].map(m=>[m.id,m])).values()].sort((a,b)=>a.created_at-b.created_at||a.id.localeCompare(b.id)):rows;
 }
 state.messageConversation=id;
}
async function loadData(){
 if(!state.project)return;
 const generation=state.generation,id=state.project.id;
 const [project,tasks,attentions,decisions,conversations,health,watchdog]=await Promise.all([api(`/projects/${id}`),api(`/tasks?project_id=${id}`),api(`/projects/${id}/attentions`),api(`/projects/${id}/decisions`),api(`/conversations?project_id=${id}`),api(`/projects/${id}/health`),api(`/projects/${id}/watchdog`)]);
 if(generation!==state.generation)return;
 Object.assign(state,{project,tasks,attentions,decisions,conversations,health,watchdog});
 if(!conversations.some(c=>c.id===state.conversationId))state.conversationId=conversations[0]?.id||'';
 if(state.conversationId){remember(`devflow-conversation:${id}`,state.conversationId);await loadConversation(state.conversationId);}
}
async function refresh(){
 if(refreshInFlight){refreshAgain=true;return;}
 refreshInFlight=true;
 try{await loadData();render();if(modalTask&&activeModal==='task'&&!submissions.size)await refreshTaskView();}catch(err){handleError(err);}
 finally{refreshInFlight=false;if(refreshAgain){refreshAgain=false;void refresh();}}
}
function scheduleRefresh(){clearTimeout(refreshTimer);refreshTimer=setTimeout(()=>void refresh(),450);}
async function eventLoop(generation){
 eventController?.abort();const controller=new AbortController();eventController=controller;
 let cursor=0,backoff=1000;
 try {
   // Catch up all pages before opening the resumable stream; retain the latest 400 for display.
   while(!controller.signal.aborted){
    const rows=await api(`/events?project_id=${state.project.id}&after=${cursor}`,{signal:controller.signal});
    if(generation!==state.generation)return;
    state.events=mergeEvents(state.events,rows);if(rows.length)cursor=rows.at(-1).sequence;
    if(rows.length<200)break;
   }
   render();
   while(!controller.signal.aborted&&generation===state.generation){
    try{
     const response=await fetch(`/api/v1/events?project_id=${state.project.id}&stream=true&after=${cursor}`,{headers:{Authorization:`Bearer ${state.token}`},signal:controller.signal,cache:'no-store'});
     if(response.ok){connection('实时事件已连接',true);backoff=1000;}
     await consumeEvents(response,event=>{if(generation!==state.generation)return;const seq=Number(event.sequence);if(!Number.isFinite(seq)||seq<=cursor)return;cursor=seq;state.events=mergeEvents(state.events,[event]);scheduleRefresh();},controller.signal);
    }catch(err){if(err.name==='AbortError')return;if(err.status===401){handleError(err);return;}connection('连接中断，正在重连',false);}
    if(controller.signal.aborted)return;
    await new Promise(resolve=>{const finish=()=>{clearTimeout(timeout);controller.signal.removeEventListener('abort',finish);resolve();};const timeout=setTimeout(finish,backoff);controller.signal.addEventListener('abort',finish,{once:true});});
    backoff=Math.min(backoff*2,15000);
   }
 }catch(err){if(err.name!=='AbortError'){connection('事件连接异常',false);handleError(err);}}
}
async function switchProject(id){
 eventController?.abort();closeModal();state.generation++;state.project=state.projects.find(p=>p.id===id)||state.projects[0]||null;state.events=[];state.tasks=[];state.messages=[];state.attentions=[];state.decisions=[];state.conversations=[];state.health=null;state.watchdog=null;state.watchdogAnswer='';state.filter='all';state.search='';
 if(state.project){remember('devflow-project',state.project.id);state.conversationId=sessionStorage.getItem(`devflow-conversation:${state.project.id}`)||'';}
 $('#app').innerHTML=ui.shell(state);
 if(!state.project){render(false);return;}
 $('#content').innerHTML='<div class="loading"><span class="spinner"></span>正在读取项目现场…</div>';
 const generation=state.generation;await loadData();if(generation!==state.generation)return;render(false);void eventLoop(generation);
}
async function boot(){
 state.page=pageFromHash();
 if(!state.token){$('#app').innerHTML=ui.login();return;}
 try{state.projects=await api('/projects');await switchProject(sessionStorage.getItem('devflow-project')||state.projects[0]?.id);}
 catch(err){if(!$('#login-form'))$('#app').innerHTML=ui.login(errorMessage(err));else $('.form-error').textContent=errorMessage(err);}
}
function showModal(html,type='form'){
 lastFocus=document.activeElement;activeModal=type;$('#modal-root').innerHTML=html;document.body.classList.add('modal-open');setTimeout(()=>$('#modal-root input, #modal-root textarea, #modal-root button')?.focus(),0);
}
function closeModal(){const had=!!activeModal;$('#modal-root').innerHTML='';activeModal='';modalTask=null;modalData=null;document.body.classList.remove('modal-open');if(had&&lastFocus?.isConnected)lastFocus.focus();}
async function openTask(id){
 const generation=state.generation;
 const [task,...resources]=await Promise.all([api(`/tasks/${id}`),...['artifacts','sessions','attempts','findings','effects','events'].map(r=>api(`/tasks/${id}/${r}`))]);
 if(generation!==state.generation)return;
 modalTask=task;modalData=Object.fromEntries(['artifacts','sessions','attempts','findings','effects','events'].map((r,i)=>[r,resources[i]]));modalTab='summary';showModal(ui.taskDialog(task,modalData,state,modalTab),'task');
}
async function refreshTaskView(){
 const id=modalTask?.id;if(!id)return;
 const generation=state.generation;
 const [task,...resources]=await Promise.all([api(`/tasks/${id}`),...['artifacts','sessions','attempts','findings','effects','events'].map(r=>api(`/tasks/${id}/${r}`))]);
 if(generation!==state.generation||modalTask?.id!==id||activeModal!=='task'||submissions.size)return;
 const root=$('#modal-root'),drafts=capture(root),scroll=$('.dialog-body',root)?.scrollTop||0;
 const forms=[...root.querySelectorAll('form')].map(f=>({id:f.id,data:{...f.dataset}}));
 modalTask=task;modalData=Object.fromEntries(['artifacts','sessions','attempts','findings','effects','events'].map((r,i)=>[r,resources[i]]));
 root.innerHTML=ui.taskDialog(task,modalData,state,modalTab);restore(root,drafts);
 for(const old of forms){const f=old.id?document.getElementById(old.id):null;if(f)Object.assign(f.dataset,old.data);}
 $('.dialog-body',root).scrollTop=scroll;
}
async function reloadTask(){if(!modalTask)return;const id=modalTask.id;await refresh();await openTask(id);}
async function ensureConversation(){if(state.conversationId)return state.conversationId;const c=await api('/conversations',{method:'POST',body:{project_id:state.project.id}});state.conversationId=c.id;state.conversations.unshift(c);remember(`devflow-conversation:${state.project.id}`,c.id);return c.id;}
const lines=text=>String(text||'').split('\n').map(s=>s.trim()).filter(Boolean);
function projectBody(form,create){const f=new FormData(form),limits={};for(const k of ['turn_seconds','task_seconds','tokens','ci_repairs'])limits[k]=Number(f.get(k));const body={policy:{allowed_actors:String(f.get('allowed_actors')).split(',').map(x=>x.trim()).filter(Boolean),agent_login:String(f.get('agent_login')||'').trim(),mode:'observe'},config:{setup_commands:lines(f.get('setup_commands')),verify_commands:lines(f.get('verify_commands')),notify_feishu:f.has('notify_feishu'),limits}};if(create)Object.assign(body,{repository:String(f.get('repository')).trim(),default_branch:String(f.get('default_branch')).trim()});return body;}
function submissionKey(form,body){const fingerprint=JSON.stringify(body);if(form.dataset.fingerprint!==fingerprint){form.dataset.fingerprint=fingerprint;form.dataset.key=crypto.randomUUID();}return form.dataset.key;}
document.addEventListener('submit',async event=>{
 const form=event.target;if(!(form instanceof HTMLFormElement))return;event.preventDefault();
 const submissionId=form.id||form.dataset.attention;
 if(submissions.has(submissionId))return;
 submissions.add(submissionId);form.dataset.busy='true';const submit=form.querySelector('[type="submit"]');if(submit)submit.disabled=true;
 try{
  const f=new FormData(form);
  if(form.id==='login-form'){
   const token=String(f.get('token')).trim();const projects=await request('/projects',{},token);state.token=token;state.projects=projects;
   if(f.has('remember'))remember('devflow-token',token);else sessionStorage.removeItem('devflow-token');
   await switchProject(sessionStorage.getItem('devflow-project')||projects[0]?.id);
  } else if(form.id==='project-create-form'){
   const project=await api('/projects',{method:'POST',body:projectBody(form,true)});state.projects=await api('/projects');closeModal();await switchProject(project.id);toast('仓库已接入。尚未派发开发任务。');
  } else if(form.id==='settings-form'){
   await api(`/projects/${state.project.id}`,{method:'PATCH',body:projectBody(form,false)});await refresh();toast('项目设置已保存。');
  } else if(form.id==='task-create-form'){
   const body={project_id:state.project.id,title:String(f.get('title')).trim(),goal:String(f.get('goal')).trim(),acceptance:lines(f.get('acceptance')),scope:f.get('scope'),priority:Number(f.get('priority')),issue_number:f.get('issue_number')?Number(f.get('issue_number')):null};
   const task=await api('/tasks',{method:'POST',body,key:submissionKey(form,body)});closeModal();await refresh();await openTask(task.id);toast('目标已派发，助手将按依赖与优先级推进。');
  } else if(form.id==='chat-form'){
   const text=String(f.get('text')).trim();if(!text)throw new Error('请输入消息内容。');
   const id=await ensureConversation(),body={text,scope:f.get('scope'),task_id:f.get('task_id')||null};
   await api(`/conversations/${id}/messages`,{method:'POST',body,key:submissionKey(form,body)});form.elements.text.value='';delete form.dataset.key;delete form.dataset.fingerprint;
   await refresh();if($('#chat-status'))$('#chat-status').textContent='消息已入队，等待项目助手处理；回复将自动显示。';
  } else if(form.dataset.form==='answer'){
   const text=String(f.get('text')).trim();if(!text)throw new Error('请填写你的决定。');
   await api(`/tasks/${form.dataset.task}/attentions/${form.dataset.attention}/answer`,{method:'POST',body:{text}});await refresh();if(modalTask)await refreshTaskView();toast('决定已保存并交回助手。');
  } else if(form.id==='watchdog-form'){
   const text=String(f.get('text')).trim();if(!text)throw new Error('请输入要询问 Watchdog 的内容。');
   const result=await api(`/projects/${state.project.id}/watchdog/ask`,{method:'POST',body:{text}});
   state.watchdogAnswer=result.answer||'';render();toast('Watchdog 已回复。');
  } else if(form.id==='watchdog-instruction-form'){
   const text=String(f.get('text')).trim();if(!text)throw new Error('请输入维护指令。');
   await api(`/projects/${state.project.id}/watchdog/instructions`,{method:'POST',body:{text}});
   form.reset();await refresh();toast('Watchdog 维护指令已记录。');
  } else if(form.id==='task-input-form'){
   const body={text:String(f.get('text')).trim()};await api(`/tasks/${modalTask.id}/inputs`,{method:'POST',body,key:submissionKey(form,body)});form.reset();toast('补充要求已进入管理队列。');
  } else if(form.id==='priority-form'){
   const body={action:'priority',priority:Number(f.get('priority'))};await api(`/tasks/${modalTask.id}/commands`,{method:'POST',body,key:submissionKey(form,body)});await reloadTask();toast('优先级已更新。');
  } else if(form.id==='dependency-form'){
   await api(`/tasks/${modalTask.id}/dependencies`,{method:'POST',body:{prerequisite_id:f.get('prerequisite_id')}});await reloadTask();toast('前置依赖已添加。');
  }
 }catch(err){if(form.id==='login-form')$('.form-error',form).textContent=errorMessage(err);else{handleError(err);if(form.isConnected){let box=$('.form-error',form);if(!box){box=document.createElement('p');box.className='form-error';box.setAttribute('role','alert');form.append(box);}box.textContent=errorMessage(err);}}}
 finally{submissions.delete(submissionId);form.dataset.busy='false';if(submit)submit.disabled=false;const current=form.id?document.getElementById(form.id):document.querySelector(`[data-attention="${form.dataset.attention}"]`);if(current){current.dataset.busy='false';current.querySelector('[type="submit"]')?.removeAttribute('disabled');}}
});
document.addEventListener('click',async event=>{
 const el=event.target.closest('[data-action],button[data-task],[data-filter],[data-suggestion],[data-tab],[data-retry-effect]');if(!el)return;
 try{
  if(el.dataset.task){await openTask(el.dataset.task);return;}
  if(el.dataset.filter){state.filter=el.dataset.filter;render();return;}
  if(el.dataset.suggestion){$('#chat-form textarea').value=el.dataset.suggestion;$('#chat-form textarea').focus();return;}
  if(el.dataset.tab){modalTab=el.dataset.tab;$('#modal-root').innerHTML=ui.taskDialog(modalTask,modalData,state,modalTab);return;}
  if(el.dataset.retryEffect){el.disabled=true;await api(`/tasks/${modalTask.id}/effects/${el.dataset.retryEffect}/retry`,{method:'POST'});await reloadTask();toast('外部操作已重新排队。');return;}
  const action=el.dataset.action;
  if(action==='logout')logout();
  if(action==='new-project')showModal(ui.modal('接入 GitHub 仓库',ui.projectForm(null,true)));
  if(action==='new-task')showModal(ui.modal('交付一个新目标',ui.taskForm()));
  if(action==='close-modal')closeModal();
  if(action==='refresh'){await refresh();toast('项目现场已更新。');}
  if(action==='toggle-pause'){
   const paused=state.health?.paused??state.project?.paused;
   const result=await api(`/projects/${state.project.id}/pause`,{method:'POST',body:{paused:!paused}});
   await refresh();toast(result.paused?'项目已暂停处理。':'项目已恢复处理。');
  }
  if(action==='new-conversation'){const c=await api('/conversations',{method:'POST',body:{project_id:state.project.id}});state.conversationId=c.id;state.conversations.unshift(c);state.messages=[];remember(`devflow-conversation:${state.project.id}`,c.id);render(false);}
  if(action==='older-messages'){await loadConversation(state.conversationId,true);render();}
  if(action?.startsWith('command-')||action==='replace-session'){
   const command=action==='replace-session'?'replace_session':action.slice(8);
   if(command==='cancel'&&!confirm('取消此任务？现场会保留，后续需要你显式恢复。'))return;
   if(command==='replace_session'&&!confirm('替换执行者并交接当前目标、代码与证据？'))return;
   const body={action:command};el.disabled=true;const key=el.dataset.key||crypto.randomUUID();el.dataset.key=key;
   await api(`/tasks/${modalTask.id}/commands`,{method:'POST',body,key});await reloadTask();toast('任务指令已执行。');
  }
 }catch(err){el.disabled=false;handleError(err);}
});
document.addEventListener('change',event=>{
 if(['project-select','compact-project-select'].includes(event.target.id))void switchProject(event.target.value).catch(handleError);
 if(event.target.id==='conversation-select'){state.conversationId=event.target.value;remember(`devflow-conversation:${state.project.id}`,state.conversationId);void loadConversation(state.conversationId).then(()=>render(false)).catch(handleError);}
});
document.addEventListener('input',event=>{if(event.target.id==='task-search'){state.search=event.target.value;render();}});
document.addEventListener('keydown',event=>{
 if(event.key==='Escape'&&activeModal)closeModal();
 if(event.key==='Enter'&&(event.ctrlKey||event.metaKey)&&event.target.closest('#chat-form')){event.preventDefault();$('#chat-form').requestSubmit();}
 if(event.key==='Tab'&&activeModal){const els=[...$('#modal-root').querySelectorAll('button:not(:disabled),input:not(:disabled),textarea,select,a[href],summary')];const first=els[0],last=els.at(-1);if(event.shiftKey&&document.activeElement===first){event.preventDefault();last?.focus();}else if(!event.shiftKey&&document.activeElement===last){event.preventDefault();first?.focus();}}
});
window.addEventListener('hashchange',()=>{state.page=pageFromHash();closeModal();render(false);});
window.addEventListener('beforeunload',()=>eventController?.abort());
setInterval(()=>{if(state.token&&state.project&&!document.hidden)void refresh();},15000);
void boot();
