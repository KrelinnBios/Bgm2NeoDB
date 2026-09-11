"use strict";
const $ = id => document.getElementById(id);
const csrf = document.querySelector('meta[name="csrf-token"]').content;
const shelfNames = {wishlist:"想看",complete:"看过",progress:"在看",dropped:"抛弃"};
let countdownInterval = null;
const statusNames = {
  pending:"待处理",
  ready:"待处理",
  writing:"待处理",
  skipped:"待处理",
  resolve_failed:"解析失败",
  failed:"失败",
  partial:"失败",
  conflict:"冲突",
  blocked_private_visibility:"隐私待确认",
  migrated:"已完成"
};
const statusGroups = [
  {id:"queued", label:"待处理", statuses:["pending","ready","writing","skipped"]},
  {id:"resolve_failed", label:"解析失败", statuses:["resolve_failed"]},
  {id:"conflict", label:"冲突", statuses:["conflict"]},
  {id:"blocked_private_visibility", label:"隐私待确认", statuses:["blocked_private_visibility"]},
  {id:"failed", label:"失败", statuses:["failed","partial"]},
  {id:"migrated", label:"已完成", statuses:["migrated"]}
];
for(const group of statusGroups){
  const option=element("option",group.label);option.value="group:"+group.id;$("filter").append(option);
  const button=element("button",undefined,"status-card secondary");button.type="button";button.dataset.group=group.id;button.setAttribute("aria-pressed","false");
  const count=element("strong","0");count.id="count-"+group.id;button.append(count,element("span",group.label));
  button.onclick=()=>{$("filter").value="group:"+group.id;$("filter").onchange();};
  $("status-counts").append(button);
}
function updateStatusSelection(){
  for(const button of $("status-counts").children)button.setAttribute("aria-pressed",String($("filter").value==="group:"+button.dataset.group));
}
const fieldNames = {shelf_type:"收藏状态",visibility:"可见性",rating_grade:"评分",comment_text:"短评",tags:"标签"};
let page=1, lastState=null, busy=false, pauseRequested=false, signature="", searchTimer, dialogPagePosition=null;
function notice(text, error=false){$("notice").textContent=text;$("notice").hidden=!text;$("notice").classList.toggle("warning",error);}
async function api(path, body, signal){
  const options=body===undefined?{}:{method:"POST",headers:{"Content-Type":"application/json","X-CSRF-Token":csrf},body:JSON.stringify(body)};
  if(signal)options.signal=signal;
  const response=await fetch(path,options);
  let result;
  try{result=await response.json();}catch{throw new Error("程序暂时无法响应，请检查它是否仍在运行。");}
  if(!response.ok)throw new Error(result.error||"操作失败，请重试。");
  return result;
}
function element(tag,text,className){const el=document.createElement(tag);if(text!==undefined)el.textContent=text;if(className)el.className=className;return el;}
function showValue(field,value){
  if(value===null||value===undefined)return "未收藏 / 无";
  if(field==="shelf_type")return shelfNames[value]||value;
  if(field==="visibility")return ["公开","仅关注者","私密"][value]||"未知";
  if(field==="rating_grade")return value?`${value} 分`:"未评分";
  if(Array.isArray(value))return value.join("、")||"无";
  return value||"无";
}
function openDialog(dialog){
  if(dialogPagePosition===null){
    dialogPagePosition={x:window.scrollX,y:window.scrollY};
    document.body.style.setProperty("--dialog-scroll-top",`${-dialogPagePosition.y}px`);
    document.body.classList.add("dialog-scroll-locked");
  }
  dialog.showModal();
}
function restoreDialogPagePosition(){
  if(document.querySelector("dialog[open]")||dialogPagePosition===null)return;
  const position=dialogPagePosition;dialogPagePosition=null;
  document.body.classList.remove("dialog-scroll-locked");
  document.body.style.removeProperty("--dialog-scroll-top");
  requestAnimationFrame(()=>window.scrollTo({left:position.x,top:position.y,behavior:"auto"}));
}
function detail(row){
  $("detail-title").textContent=row.title;
  const box=$("detail-content");box.replaceChildren();
  const links=element("div",undefined,"detail-links");
  for(const [name,url] of [["Bangumi 原条目",row.source_url],["NeoDB 对应条目",row.target_url]]){
    if(!url)continue;const a=element("a",name,"button secondary");a.href=url;a.target="_blank";a.rel="noopener noreferrer";links.append(a);
  }
  const platform=String(row.source_platform||"").toLowerCase();
  let createType={1:"Edition",3:"Album",4:"Game"}[row.source_type];
  if(row.source_type===2){
    createType=/movie|film|剧场版|电影/.test(platform)?"Movie":"TVShow";
  }else if(row.source_type===6){
    if(/movie|film|剧场版|电影/.test(platform))createType="Movie";
    else if(/tv|web|drama|电视剧|连续剧/.test(platform))createType="TVShow";
    else if(/performance|stage|concert|live|舞台|演唱会|现场/.test(platform))createType="Performance";
    else createType=null;
  }
  const neoInstance=lastState?.neodb?.instance;
  if(createType&&neoInstance){
    const create=element("a","在 NeoDB 创建条目","button secondary");
    create.href=`${neoInstance.replace(/\/$/,"")}/catalog/create/${createType}?title=${encodeURIComponent(row.title)}`;
    create.target="_blank";create.rel="noopener noreferrer";links.append(create);
  }
  box.append(links);
  if(row.error && !["resolve_failed","conflict"].includes(row.status))box.append(element("p",row.error,"notice warning"));
  if(["resolve_failed","conflict"].includes(row.status)){
    const mapBox=element("div",undefined,"map-box");
    const guidance=row.status === "conflict"
      ? "请确认要使用的作品或版本。"
      : row.resolution?.message||"请核对候选作品，或粘贴 NeoDB 条目链接或外部来源链接。";
    const guidanceNode=element("p",guidance,"muted");mapBox.append(guidanceNode);
    const incompleteHint=row.resolution?.code === "incomplete"
      ? "搜索结果还没返回完整，暂不自动选择，请从候选中确认或重新查找。"
      : "";
    if(incompleteHint)guidanceNode.hidden=true;
    const feedback=element("p",undefined,"notice map-feedback");feedback.hidden=true;feedback.setAttribute("role","status");feedback.setAttribute("aria-live","polite");
    const showFeedback=(message,error=false)=>{guidanceNode.hidden=true;feedback.textContent=message;feedback.hidden=false;feedback.classList.toggle("warning",error);};
    if(incompleteHint)showFeedback(incompleteHint,true);
    const mapAction=async(button,label,fn)=>{
      if(busy){showFeedback("正在处理上一项操作，请稍候再试。",true);return;}
      busy=true;const original=button.textContent;
      for(const control of mapBox.querySelectorAll("button,input"))control.disabled=true;
      button.textContent=label;showFeedback(label);
      try{await fn();await refresh(true);}catch(error){showFeedback(error.message,true);}
      finally{busy=false;button.textContent=original;for(const control of mapBox.querySelectorAll("button,input"))control.disabled=false;await refresh().catch(()=>{});}
    };
    const searchBtn=element("button","重新查找候选","secondary");searchBtn.type="button";
    const list=element("ul",undefined,"map-list");
    const renderCandidates=res=>{
      list.replaceChildren();
      if(!res.candidates.length)list.append(element("li","未找到候选条目，可粘贴作品外链，让 NeoDB 抓取并创建条目。","muted"));
      for(const candidate of res.candidates){
        const li=element("li"),info=element("a",`${candidate.title}（${candidate.type}）${res.exact===candidate.url?" · 已匹配":""}`);
        info.href=candidate.url;info.target="_blank";info.rel="noopener noreferrer";
        const details=element("div",undefined,"candidate-details");details.append(info);
        const metadata=candidate.metadata||{},facts=[];
        const date=metadata.release_date||metadata.year||metadata.release_year;
        if(date)facts.push(`发行：${date}`);
        if(Number.isInteger(metadata.season_number))facts.push(`第 ${metadata.season_number} 季`);
        if(Number.isInteger(metadata.episode_count))facts.push(`${metadata.episode_count} 集`);
        for(const [key,label] of [["director","导演"],["actor","演员"],["developer","开发"],["publisher","发行方"],["platform","平台"]]){
          const value=metadata[key];if(value?.length)facts.push(`${label}：${Array.isArray(value)?value.join("、"):value}`);
        }
        details.append(element("p",facts.join(" · ")||"暂无年份、季数等版本资料","candidate-meta"));
        if(metadata.orig_title&&metadata.orig_title!==candidate.title)details.append(element("p",`原名：${metadata.orig_title}`,"candidate-meta"));
        const description=metadata.description||metadata.brief;
        if(description){const more=element("details",undefined,"candidate-description");more.append(element("summary","查看简介"),element("p",description));details.append(more);}
        const pick=element("button","确认选择","candidate-pick");pick.type="button";
        pick.onclick=()=>mapAction(pick,"正在保存…",async()=>{
          await api(`/api/map/${row.id}`,{url:candidate.url});
          await api(`/api/jobs/item/${row.id}`,{import_date:$("import-date").checked});
          $("detail-dialog").close();
        });
        li.append(details,pick);list.append(li);
      }
    };
    renderCandidates({candidates:row.resolution?.candidates||[],exact:row.target_url});
    searchBtn.onclick=()=>mapAction(searchBtn,"正在查找…",async()=>{
        const res=await api(`/api/map/candidates/${row.id}`);
        renderCandidates(res);
        showFeedback(res.resolution?.message||`找到 ${res.candidates.length} 个候选，请核对后选择。`);
    });
    const input=element("input");input.type="text";input.id="map-url";input.value=row.resolution?.pending_url||"";input.placeholder="https://bgm.tv/subject/…";
    const inputLabel=element("label","粘贴作品链接（NeoDB 或外部来源）","map-input-label");inputLabel.htmlFor=input.id;
    const linkBtn=element("button","按链接导入","secondary");linkBtn.type="button";
    linkBtn.onclick=()=>mapAction(linkBtn,"正在解析链接…",async()=>{
      const value=input.value.trim();
      if(!value){showFeedback("请先粘贴 NeoDB 条目链接或外部来源链接。",true);return;}
      const deadline=Date.now()+60000,controller=new AbortController();
      let message="正在提交给 NeoDB 解析链接…",retryTimer;
      const updateCountdown=()=>{
        showFeedback(message);
        feedback.append(element("span",` 剩余 ${Math.max(0,Math.ceil((deadline-Date.now())/1000))} 秒`,"map-countdown"));
      };
      updateCountdown();
      const countdown=setInterval(updateCountdown,1000);
      let timeout;
      const expired=new Promise((_,reject)=>{timeout=setTimeout(()=>{
        controller.abort();
        reject(new Error("等待已满 60 秒，已停止自动重试。"));
      },60000);});
      try{
        await Promise.race([expired,(async()=>{
          let result=await api(`/api/map/${row.id}`,{url:value},controller.signal);
          while(result.pending){
            message="NeoDB 正在抓取该外链，稍后会自动重试。";updateCountdown();
            await new Promise(resolve=>{retryTimer=setTimeout(resolve,Math.max(15,Number(result.retry_after)||15)*1000);});
            if(Date.now()>=deadline||controller.signal.aborted)return;
            result=await api(`/api/map/${row.id}/pending`,undefined,controller.signal);
          }
        })()]);
        if(Date.now()>=deadline)throw new Error("等待已满 60 秒，已停止自动重试。");
      }catch(error){
        throw new Error(`${controller.signal.aborted?"等待已满 60 秒，已停止自动重试。":error.message} 请在 NeoDB 手动创建该条目，再粘贴 NeoDB 条目链接。`);
      }finally{
        clearTimeout(timeout);clearTimeout(retryTimer);clearInterval(countdown);
      }
      await api(`/api/jobs/item/${row.id}`,{import_date:$("import-date").checked});
      $("detail-dialog").close();
    });
    input.onkeydown=event=>{if(event.key==="Enter"){event.preventDefault();linkBtn.click();}};
    const linkRow=element("div",undefined,"map-link");linkRow.append(input,linkBtn);
    const candidateHeading=element("div",undefined,"candidate-heading");candidateHeading.append(element("strong","候选条目"),searchBtn);
    mapBox.append(inputLabel,linkRow,feedback,candidateHeading,list);
    box.append(mapBox);
  }
  if(row.plan){
    const table=element("table"),head=element("tr");for(const t of ["字段","NeoDB 当前","迁移后"])head.append(element("th",t));table.append(head);
    for(const field of Object.keys(fieldNames)){
      const tr=element("tr");tr.append(element("td",fieldNames[field]+(row.plan.diff[field]?" · 更新写入":" · 原值写入")),element("td",showValue(field,row.plan.before?.[field])),element("td",showValue(field,row.plan.after[field])));table.append(tr);
    }box.append(table);
    for(const note of row.plan.notes)box.append(element("p",note,"muted"));
  }
  openDialog($("detail-dialog"));
}
async function loadEntries(){
  const result=await api(`/api/entries?page=${page}&filter=${$("filter").value}&q=${encodeURIComponent($("search").value)}`);
  const body=$("entries");body.replaceChildren();
  for(const row of result.entries){
    const tr=element("tr"),title=element("td",row.title);title.append(element("small",`#${row.id}${row.private?" · 私密收藏":""}`));
    const source=element("td",row.source_status);source.append(element("small",row.source_rating?`${row.source_rating} 分`:"未评分"));
    const after=element("td",row.plan?showValue("shelf_type",row.plan.after.shelf_type):"等待解析");
    if(row.plan)after.append(element("small",showValue("rating_grade",row.plan.after.rating_grade)));
    const state=element("td"),badge=element("span",statusNames[row.status]||row.status,"badge");
    if(["failed","partial","conflict","resolve_failed","blocked_private_visibility"].includes(row.status))badge.classList.add("error");
    if(["pending","writing"].includes(row.status))badge.classList.add("wait");state.append(badge);
    const action=element("td"),button=element("button","详情","secondary");button.addEventListener("click",()=>detail(row));action.append(button);
    tr.append(title,source,after,state,action);body.append(tr);
  }
  $("empty").hidden=!!result.entries.length;
  const pages=Math.max(1,Math.ceil(result.total/30));$("page-label").textContent=`共 ${result.total} 项 · 第 ${page} / ${pages} 页`;
  $("prev").disabled=page<=1;$("next").disabled=page>=pages;
}
async function refresh(force=false){
  const state=await api("/api/state");lastState=state;
  const running=state.job.running, connected=!!(state.bangumi&&state.neodb), summary=state.summary;
  $("bgm-user").textContent=state.bangumi?`已连接 · ${state.bangumi.nickname||state.bangumi.username}`:"尚未连接账号";
  $("neo-user").textContent=state.neodb?`已连接 · ${state.neodb.user.display_name||state.neodb.user.external_acct||"NeoDB 用户"}`:"尚未连接账号";
  $("bgm-connect").textContent=state.bangumi?"更换账号":"连接 Bangumi";$("neo-connect").textContent=state.neodb?"更换账号":"连接 NeoDB";
  $("bgm-disconnect").hidden=!state.bangumi;$("neo-disconnect").hidden=!state.neodb;
  for(const id of ["bgm-connect","neo-connect","bgm-disconnect","neo-disconnect"])$(id).disabled=running||busy;
  $("warning").hidden=!state.warning;$("warning").textContent=state.warning;
  const remaining=summary.pending||summary.ready||summary.skipped||summary.needs_attention;
  const retryable=summary.retryable??remaining;
  if(!running)pauseRequested=false;
  $("scan").disabled=busy||pauseRequested||(!running&&(!connected||(state.has_snapshot&&!retryable)));
  $("scan").textContent=running?(pauseRequested?"正在暂停…":"暂停"):state.has_snapshot?(retryable?"继续自动迁移 →":remaining?"请先确认待处理条目":"迁移完成"):"开始自动迁移 →";
  $("rescan").disabled=running||busy||!connected;
  $("rescan").title=running?"请先暂停当前任务，再重新扫描":"重新读取 Bangumi 收藏并迁移";
  $("workspace-title").textContent=running?"正在处理你的收藏":state.has_snapshot?"查看迁移进度与结果":connected?"账号已连接，开始自动迁移":"连接账号后，扫描你的收藏";
  $("progress-box").hidden=!running&&!state.job.message;
  // 处理倒计时和总用时
  if(state.job.countdown_deadline){
    if(!countdownInterval){
      countdownInterval=setInterval(()=>{
        const remaining=Math.max(0,Math.ceil(state.job.countdown_deadline-Date.now()/1000));
        const elapsed=state.job.phase_start_time?Math.floor(Date.now()/1000-state.job.phase_start_time):0;
        const elapsedText=elapsed>0?` · 总用时 ${elapsed} 秒`:"";
        $("job-label").textContent=`快速条目已处理，正在处理剩余条目（当前条目剩余 ${remaining} 秒${elapsedText}）；已开始写入的条目会继续核对…`;
      },1000);
    }
    const remaining=Math.max(0,Math.ceil(state.job.countdown_deadline-Date.now()/1000));
    const elapsed=state.job.phase_start_time?Math.floor(Date.now()/1000-state.job.phase_start_time):0;
    const elapsedText=elapsed>0?` · 总用时 ${elapsed} 秒`:"";
    $("job-label").textContent=`快速条目已处理，正在处理剩余条目（当前条目剩余 ${remaining} 秒${elapsedText}）；已开始写入的条目会继续核对…`;
  }else{
    if(countdownInterval){clearInterval(countdownInterval);countdownInterval=null;}
    $("job-label").textContent=state.job.message||"自动迁移中";
  }
  $("job-count").textContent=`${state.job.done} / ${state.job.total}`;
  for(const id of ["job-count","progress","job-title"])$(id).hidden=!running;
  $("progress").max=Math.max(1,state.job.total);$("progress").value=state.job.done;$("job-title").textContent=state.job.title;
  $("stats").hidden=!state.has_snapshot;$("preview-box").hidden=!state.has_snapshot;
  $("total").textContent=summary.total;
  for(const group of statusGroups)$("count-"+group.id).textContent=group.statuses.reduce((n,status)=>n+(summary[status]??0),0);
  $("import-date").disabled=running||busy;
  const {generated_at: reportTime, ...stableSummary}=summary;
  const nextSignature=JSON.stringify([stableSummary,state.job.done,running]);
  if(force||nextSignature!==signature){signature=nextSignature;if(state.has_snapshot)await loadEntries();}
}
async function action(fn){
  if(busy)return;busy=true;
  try{await fn();await refresh(true);}catch(error){notice(error.message,true);}finally{busy=false;await refresh().catch(()=>{});}
}
for(const button of document.querySelectorAll(".close"))button.onclick=()=>button.closest("dialog").close();
for(const dialog of document.querySelectorAll("dialog"))dialog.addEventListener("close",restoreDialogPagePosition);
$("bgm-connect").onclick=()=>openDialog($("bgm-dialog"));
$("neo-connect").onclick=()=>{$("neo-instance").value=lastState?.neodb?.instance||"https://neodb.social";openDialog($("neo-dialog"));};
$("bgm-dialog").addEventListener("close",()=>{$("bgm-token").value="";});
$("bgm-form").onsubmit=event=>{event.preventDefault();action(async()=>{const token=$("bgm-token").value;$("bgm-token").value="";$("bgm-dialog").close();await api("/api/connect/bangumi",{token});});};
$("neo-form").onsubmit=event=>{event.preventDefault();action(async()=>{$("neo-dialog").close();const result=await api("/api/connect/neodb",{instance:$("neo-instance").value});window.location.assign(result.url);});};
for(const platform of ["bgm","neo"])$(platform+"-disconnect").onclick=()=>action(()=>api("/api/disconnect/"+(platform==="bgm"?"bangumi":"neodb"),{}));
$("scan").onclick=()=>action(async()=>{
  if(lastState?.job.running){await api("/api/pause",{});pauseRequested=true;}
  else{await api("/api/jobs/auto",{import_date:$("import-date").checked});page=1;}
});
$("rescan").onclick=()=>action(async()=>{
  await api("/api/jobs/scan",{import_date:$("import-date").checked});page=1;
});
$("filter").onchange=()=>{updateStatusSelection();page=1;loadEntries().catch(e=>notice(e.message,true));};
const searchInput=$("search"),clearSearch=$("clear-search");
function updateSearchClear(){clearSearch.hidden=!searchInput.value;}
searchInput.oninput=()=>{updateSearchClear();clearTimeout(searchTimer);searchTimer=setTimeout(()=>{page=1;loadEntries().catch(e=>notice(e.message,true));},250);};
clearSearch.onclick=()=>{searchInput.value="";updateSearchClear();searchInput.dispatchEvent(new Event("input",{bubbles:true}));searchInput.focus();};
$("prev").onclick=()=>{page--;loadEntries().catch(e=>notice(e.message,true));};$("next").onclick=()=>{page++;loadEntries().catch(e=>notice(e.message,true));};
async function poll(){
  try{
    await refresh();
    // 检查是否有遗留的滚动锁定但没有打开的对话框
    if(document.body.classList.contains("dialog-scroll-locked")&&!document.querySelector("dialog[open]")){
      document.body.classList.remove("dialog-scroll-locked");
      document.body.style.removeProperty("--dialog-scroll-top");
      dialogPagePosition=null;
    }
  }catch(error){notice(error.message,true);}
  setTimeout(poll,2000);
}
poll();
