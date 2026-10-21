'use strict';
const $ = id => document.getElementById(id);
const escapeHTML = value => String(value ?? '').replace(/[&<>"']/g, c => ({'&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;',"'":'&#39;'}[c]));
const state = {user:null,role:'student',submissions:[],submission:null,page:1,selected:null,zoom:1,draw:null,box:null,rebuttal:null,reviewEdit:null,busy:false,load:0};
let toastTimer;
function toast(message, error=false) { $('toast').textContent=message; $('toast').classList.toggle('error-toast',error); $('toast').hidden=false; clearTimeout(toastTimer); toastTimer=setTimeout(()=>$('toast').hidden=true,5500); }
async function api(path, data) {
  const response = await fetch(path, {credentials:'same-origin',headers:{'Content-Type':'application/json',...(state.user ? {'X-CSRF-Token':state.user.csrf} : {})},...(data===undefined?{}:{method:'POST',body:JSON.stringify(data)})});
  const value=await response.json();
  if(!response.ok){if(response.status===401 && path!=='/api/login')showLogin(); const error=new Error(value.error || 'Something went wrong. Please try again.');error.status=response.status;throw error;}
  return value;
}
function showLogin(){if($('delete-batch-dialog').open)$('delete-batch-dialog').close();if($('batches-dialog').open)$('batches-dialog').close();$('batch-list').replaceChildren();clearStudentAccess();state.user=null;state.submission=null;$('workspace').hidden=true;$('login-view').hidden=false;}
function setRole(role){state.role=role;document.querySelectorAll('[data-role]').forEach(button=>{button.classList.toggle('active',button.dataset.role===role);button.setAttribute('aria-pressed',button.dataset.role===role)});document.querySelector('label[for="identifier"]').textContent=role==='student'?'Student identifier':'Instructor identifier';$('identifier').placeholder=role==='student'?'e.g. piermaeryn':'e.g. instructor1';$('login-error').textContent='';}
document.querySelectorAll('[data-role]').forEach(button=>button.addEventListener('click',()=>setRole(button.dataset.role)));
$('login-form').addEventListener('submit',async event=>{event.preventDefault(); const button=event.submitter;button.disabled=true;$('login-error').textContent='';try{state.user=await api('/api/login',{identifier:$('identifier').value.trim(),key:$('access-key').value.trim(),role:state.role});$('access-key').value='';await enterWorkspace();}catch(error){$('login-error').textContent=error.message;}finally{button.disabled=false;}});
$('logout').addEventListener('click',async()=>{try{await api('/api/logout',{});showLogin();}catch(error){toast(error.message,true)}});
async function enterWorkspace(){ $('login-view').hidden=true;$('workspace').hidden=false;$('portal-label').textContent=state.user.role+' portal';$('finding-filter').querySelector('option[value="pending"]').textContent=state.user.role==='student'?'Instructor reviewed':'Awaiting review';$('account-name').textContent=state.user.id;$('workspace-eyebrow').textContent=state.user.role==='student'?'STUDENT WORKSPACE':'INSTRUCTOR WORKSPACE';$('import-data').hidden=state.user.role!=='instructor';$('upload-evaluation').hidden=state.user.role!=='instructor';$('student-access').hidden=state.user.role!=='instructor';$('manage-batches').hidden=state.user.role!=='instructor';$('report-mistake').closest('.report-footer').hidden=state.user.role!=='student'; await loadSubmissions(); }
async function loadSubmissions(preferred){state.submissions=await api('/api/submissions');const select=$('submission-select');select.replaceChildren();for(const item of state.submissions){const option=document.createElement('option');option.value=item.id;option.textContent=`${item.student_id} · ${item.label}${item.pending?' · '+item.pending+' pending':''}`;select.append(option)}$('empty-state').hidden=!!state.submissions.length;$('review-layout').hidden=!state.submissions.length;if(state.submissions.length){select.value=state.submissions.some(item=>item.id===preferred)?preferred:state.submissions[0].id;await selectSubmission(select.value);}else{state.submission=null;state.selected=null;$('page-image').removeAttribute('src');$('findings').replaceChildren();$('boxes').replaceChildren();$('student-heading').textContent='Drawing review';$('assignment-label').textContent='';$('original-score').textContent='—';$('adjusted-score').textContent='—';$('notes').hidden=true;$('empty-state').textContent=state.user.role==='student'?'No evaluations are currently available. Your instructor can make a batch visible.':'No drawings have been assigned yet.';} }
async function selectSubmission(sid){cancelDraw();state.rebuttal=null;state.reviewEdit=null;state.selected=null;state.page=1;state.zoom=1;await loadSubmission(sid,true);}
async function loadSubmission(sid=state.submission?.id,resetImage=false){if(!sid)return;const load=++state.load;const value=await api('/api/submission?submission='+encodeURIComponent(sid));if(load!==state.load)return;state.submission=value;if(!value.findings.some(item=>item.id===state.selected))state.selected=null;state.page=Math.min(state.page,value.pages);renderHeader();renderFindings();if(resetImage)renderPage();else renderBoxes();}
function formatScore(value){return Number(value.toFixed(2)).toString()}
function renderHeader(){const data=state.submission;const summary=state.submissions.find(item=>item.id===data.id);$('student-heading').textContent=data.student_id;$('assignment-label').textContent=(summary?.label || 'Drawing evaluation')+' / '+data.pages+' pages';$('original-score').innerHTML=`${formatScore(data.scores.original)} <small>/ ${data.scores.maximum}</small>`;$('adjusted-score').innerHTML=`${formatScore(data.scores.adjusted)} <small>/ ${data.scores.maximum}</small>`;$('notes').hidden=!data.notes;$('notes').textContent=data.notes||'';$('download-pdf').href='/api/pdf?submission='+encodeURIComponent(data.id);const rules=data.scores.rules;$('rules-content').innerHTML=`<p>Each unique AI mistake starts at <strong>${escapeHTML(rules.base_deduction)} point(s)</strong>. Further occurrences of the same catalog ID use a <strong>${escapeHTML(rules.repeat_decay)} multiplier</strong>: ${[0,1,2].map(n=>formatScore(rules.base_deduction*rules.repeat_decay**n)).join(', ')}…</p><p>A finding with multiple highlighted regions counts once. Accepted rebuttals remove findings, then deductions are recalculated.</p><p>Accepted self-reports add <strong>${escapeHTML(rules.self_report_credit)} point(s)</strong>, with the same repeat decay by catalog ID. Unclassified self-reports share one repeat group. Scores stay between 0 and ${escapeHTML(rules.maximum)}.</p><p class="muted">Self-report credits earned so far: ${escapeHTML(data.scores.credit)}. Pending requests do not change points.</p>`;}
function statusText(finding){if(finding.status==='pending')return finding.kind==='ai'?'Rebuttal in progress':'Self-report in progress';if(finding.status==='accepted')return finding.kind==='ai'?'Rebuttal accepted · finding removed':'Self-report accepted · credit added';if(finding.status==='rejected')return finding.kind==='ai'?'! Rebuttal unsuccessful':'! Self-report unsuccessful';return 'AI finding';}
function findingHTML(finding){const active=state.selected===finding.id;const isStudent=state.user.role==='student';const catalog=state.submission.catalog.find(item=>item.id===finding.catalog_id);const code=finding.catalog_id || 'SELF';const title=finding.kind==='self'?finding.description:finding.title;let body='';if(active){body=`<div class="finding-body">${finding.kind==='ai'?`<p class="eyebrow">AI OBSERVATION · ${escapeHTML(finding.confidence)} CONFIDENCE</p><p>${escapeHTML(finding.description)}</p>`:''}${catalog?`<details class="catalog-detail"><summary>Catalog definition</summary><p>${escapeHTML(catalog.description)}</p>${catalog.example?`<p><strong>Example:</strong> ${escapeHTML(catalog.example)}</p>`:''}</details>`:''}<div class="evidence-location">${finding.boxes.length?'⌖ '+finding.boxes.length+' highlighted region'+(finding.boxes.length===1?'':'s')+' on page '+finding.page:'No location was returned by the AI for this finding.'}</div>`;
  if(finding.status!=='open'){body+=`<div class="status-banner ${finding.kind} ${finding.status}">${escapeHTML(statusText(finding))}</div><p class="eyebrow">${finding.kind==='ai'?'STUDENT REBUTTAL':'STUDENT OBSERVATION'}</p><div class="request-text">${escapeHTML(finding.argument || finding.description)}</div>`;}
  if(finding.feedback){body+=`<p class="eyebrow">INSTRUCTOR FEEDBACK · ${escapeHTML(finding.reviewer)}</p><div class="instructor-text">${escapeHTML(finding.feedback)}</div>`;}
  if(isStudent && finding.status==='open' && finding.kind==='ai'){if(state.rebuttal===finding.id){body+=`<label for="rebuttal-text">Why do you disagree with this finding?</label><textarea id="rebuttal-text" maxlength="6000" rows="4" placeholder="Explain your reasoning and refer to the drawing…"></textarea><button class="rebuttal-button" data-action="submit-rebuttal">Submit rebuttal</button><button class="quiet" data-action="close-rebuttal">Cancel draft</button>`;}else body+=`<button class="rebuttal-button" data-action="open-rebuttal">Rebuttal</button>`;}
  if(isStudent && finding.status==='pending'){body+=`<div class="actions">${finding.kind==='self'?'<button class="quiet" data-action="revise-box">Revise bounding box</button>':''}<button class="rebuttal-button cancel-pending" data-action="cancel-request">${finding.kind==='ai'?'Rebuttal in progress · cancel':'Cancel self-report'}</button></div>`;}
  if(!isStudent && ['accepted','rejected'].includes(finding.status) && state.reviewEdit!==finding.id){body+=`<button class="edit-decision-button" data-action="edit-decision">Edit decision</button>`;}
  if(!isStudent && (finding.status==='pending' || state.reviewEdit===finding.id)){const editing=finding.status!=='pending';body+=`${editing?'<p class="eyebrow">REVISE INSTRUCTOR DECISION</p>':''}<label for="instructor-feedback">${editing?'Update your feedback to the student':'Your feedback to the student'}</label><textarea id="instructor-feedback" rows="4" maxlength="6000" placeholder="Explain your decision…">${editing?escapeHTML(finding.feedback):''}</textarea><div class="actions"><button class="agree" data-action="agree">${editing?'Save as agreed':'Agree with student'}</button><button class="disagree" data-action="disagree">${editing?'Save as disagreed':'Disagree'}</button></div>${editing?'<button class="quiet" data-action="cancel-edit-decision">Cancel edit</button>':''}`;}
  body+='</div>';
}return `<article class="finding ${finding.kind} ${finding.status}${active?' active':''}" data-id="${escapeHTML(finding.id)}"><button class="finding-summary" aria-expanded="${active}"><span class="finding-code">${finding.status==='rejected'?'! ':''}${escapeHTML(code)}</span><span><span class="finding-title">${escapeHTML(title)}</span><span class="finding-meta">Page ${finding.page} · ${escapeHTML(statusText(finding))}</span></span><span class="finding-chevron">${active?'−':'+'}</span></button>${body}</article>`;}
function instructorReviewed(item){return Boolean(item.reviewer)&&['accepted','rejected'].includes(item.status);}
function visibleFindings(){const mode=$('finding-filter').value;return state.submission.findings.filter(item=>mode==='all'||(mode==='pending'?(state.user.role==='student'?instructorReviewed(item):item.status==='pending'):item.page===state.page));}
function renderFindings(){if(!state.submission)return;const findings=visibleFindings();$('finding-count').textContent=state.submission.findings.length;$('pending-count').textContent=state.submission.findings.filter(item=>item.status==='pending').length+' pending';$('findings').innerHTML=findings.length?findings.map(findingHTML).join(''):'<div class="empty">'+($('finding-filter').value==='pending'?(state.user.role==='student'?'No instructor-reviewed findings yet.':'No requests are awaiting review.'):state.user.role==='student'?'No findings on this page. You can report an unidentified mistake below.':'No findings on this page.')+'</div>';$('report-mistake').disabled=!!state.draw;}
function updateNavigation(){const data=state.submission;$('page-label').textContent=`Page ${state.page} of ${data.pages}`;$('sheet-title').textContent=`Sheet ${String(state.page).padStart(2,'0')}`;$('previous-page').disabled=state.page<=1;$('next-page').disabled=state.page>=data.pages;$('page-findings').textContent=data.findings.filter(item=>item.page===state.page).length+' findings on this sheet';}
function renderPage(){updateNavigation();$('image-status').textContent='Loading drawing…';$('image-status').hidden=false;$('boxes').replaceChildren();$('page-image').src='/api/page?submission='+encodeURIComponent(state.submission.id)+'&page='+state.page+'&width=2400';}
$('page-image').addEventListener('load',()=>{$('image-status').hidden=true;fitSheet();renderBoxes();});
$('page-image').addEventListener('error',()=>{$('image-status').textContent='Could not load the drawing. Use Refresh feedback to try again.';$('image-status').hidden=false;});
function fitSheet(){const image=$('page-image');if(!image.naturalWidth)return;const scroll=$('canvas-scroll');const padding=getComputedStyle(scroll);const innerWidth=scroll.clientWidth-parseFloat(padding.paddingLeft)-parseFloat(padding.paddingRight);const innerHeight=scroll.clientHeight-parseFloat(padding.paddingTop)-parseFloat(padding.paddingBottom);const fitWidth=Math.max(150,Math.min(innerWidth,innerHeight*image.naturalWidth/image.naturalHeight));$('sheet').style.width=(fitWidth*state.zoom)+'px';$('zoom-label').textContent=state.zoom===1?'Fit':Math.round(state.zoom*100)+'%';}
window.addEventListener('resize',fitSheet);
function boxRect(box,classes){const [y,x,y2,x2]=box;return `<rect x="${x}" y="${y}" width="${x2-x}" height="${y2-y}" class="${classes}"></rect>`;}
function renderBoxes(){let html='';const finding=state.submission?.findings.find(item=>item.id===state.selected);if(finding?.page===state.page){html=finding.boxes.filter(item=>item.coordinate_space==='page_normalized_1000'&&Array.isArray(item.box_2d)&&item.box_2d.length===4&&item.box_2d.every(n=>Number.isFinite(n)&&n>=0&&n<=1000)).map(item=>boxRect(item.box_2d,'location-box '+finding.kind+' '+finding.status)).join('');}if(state.box)html+=boxRect(state.box,'draw-box');$('boxes').innerHTML=html;}
function hasDraft(){return Boolean(state.draw || state.rebuttal || $('instructor-feedback')?.value.trim());}
function goToPage(page){cancelDraw();state.page=page;state.selected=null;state.rebuttal=null;state.reviewEdit=null;state.zoom=1;renderPage();renderFindings();}
$('previous-page').addEventListener('click',()=>goToPage(state.page-1));$('next-page').addEventListener('click',()=>goToPage(state.page+1));
$('zoom-in').addEventListener('click',()=>{state.zoom=Math.min(4,state.zoom+.25);fitSheet()});$('zoom-out').addEventListener('click',()=>{state.zoom=Math.max(.5,state.zoom-.25);fitSheet()});$('fit').addEventListener('click',()=>{state.zoom=1;fitSheet()});
$('submission-select').addEventListener('change',async event=>{try{await selectSubmission(event.target.value)}catch(error){toast(error.message,true)}});
$('finding-filter').addEventListener('change',()=>{state.rebuttal=null;state.reviewEdit=null;renderFindings()});
$('findings').addEventListener('click',async event=>{const article=event.target.closest('[data-id]');if(!article || state.busy)return;const finding=state.submission.findings.find(item=>item.id===article.dataset.id);if(event.target.closest('.finding-summary')){cancelDraw();state.selected=state.selected===finding.id?null:finding.id;state.rebuttal=null;state.reviewEdit=null;if(state.selected && state.page!==finding.page){state.page=finding.page;state.zoom=1;renderPage();}renderFindings();renderBoxes();return;}
  const action=event.target.closest('[data-action]')?.dataset.action;if(!action)return;
  if(action==='open-rebuttal'){state.rebuttal=finding.id;renderFindings();$('rebuttal-text').focus();return;}
  if(action==='close-rebuttal'){state.rebuttal=null;state.reviewEdit=null;renderFindings();return;}
  if(action==='edit-decision'){state.reviewEdit=finding.id;renderFindings();$('instructor-feedback').focus();return;}
  if(action==='cancel-edit-decision'){state.reviewEdit=null;renderFindings();return;}
  if(action==='revise-box'){startDraw(finding.id);return;}
  const payload={finding:finding.id,version:finding.version};
  if(action==='submit-rebuttal'){const text=$('rebuttal-text').value.trim();if(!text)return toast('Explain why you disagree before submitting.',true);Object.assign(payload,{action:'rebuttal',argument:text});}
  if(action==='cancel-request')payload.action='cancel';
  if(action==='agree'||action==='disagree'){const feedback=$('instructor-feedback').value.trim();if(!feedback)return toast('Add feedback for the student before deciding.',true);Object.assign(payload,{action:'review',agree:action==='agree',feedback});}
  try{state.busy=true;event.target.disabled=true;await api('/api/action',payload);state.rebuttal=null;state.reviewEdit=null;await loadSubmission();toast(action==='cancel-request'?'Request canceled.':action==='submit-rebuttal'?'Rebuttal submitted for instructor review.':'Decision saved and shared with the student.');}catch(error){toast(error.message,true);event.target.disabled=false;}finally{state.busy=false;}
});
function startDraw(finding=null){state.rebuttal=null;state.reviewEdit=null;state.draw={finding};state.box=null;$('sheet').classList.add('drawing');$('draw-banner').hidden=false;$('draw-instruction').textContent=finding?'Draw a replacement box. Only the new region will be kept.':'First, drag one box around the unidentified mistake.';$('report-mistake').disabled=true;$('sheet').scrollIntoView({block:'nearest'});renderBoxes();}
function cancelDraw(){state.draw=null;state.box=null;$('sheet').classList.remove('drawing');$('draw-banner').hidden=true;$('report-mistake').disabled=false;renderBoxes();}
$('cancel-draw').addEventListener('click',cancelDraw);$('report-mistake').addEventListener('click',()=>{
  $('draw-help-dialog').showModal();
  $('draw-help-understood').focus();
});
$('draw-help-understood').addEventListener('click',()=>{
  $('draw-help-dialog').close();
  startDraw();
});
function point(event){const rect=$('sheet').getBoundingClientRect();return [Math.max(0,Math.min(1000,(event.clientY-rect.top)/rect.height*1000)),Math.max(0,Math.min(1000,(event.clientX-rect.left)/rect.width*1000))];}
let pointerStart=null;
$('boxes').addEventListener('pointerdown',event=>{if(!state.draw||state.busy||event.button!==0)return;event.preventDefault();pointerStart=point(event);$('boxes').setPointerCapture(event.pointerId);});
$('boxes').addEventListener('pointermove',event=>{if(!pointerStart)return;const end=point(event);state.box=[Math.min(pointerStart[0],end[0]),Math.min(pointerStart[1],end[1]),Math.max(pointerStart[0],end[0]),Math.max(pointerStart[1],end[1])];renderBoxes();});
$('boxes').addEventListener('pointercancel',()=>{pointerStart=null;state.box=null;renderBoxes()});
$('boxes').addEventListener('pointerup',async event=>{if(!pointerStart)return;const end=point(event);state.box=[Math.min(pointerStart[0],end[0]),Math.min(pointerStart[1],end[1]),Math.max(pointerStart[0],end[0]),Math.max(pointerStart[1],end[1])];pointerStart=null;renderBoxes();if(state.box[2]-state.box[0]<3 || state.box[3]-state.box[1]<3){state.box=null;renderBoxes();return toast('Drag a larger region around the mistake.',true);}if(state.draw.finding){const finding=state.submission.findings.find(item=>item.id===state.draw.finding);try{state.busy=true;await api('/api/action',{action:'edit_box',finding:finding.id,version:finding.version,box:state.box});cancelDraw();await loadSubmission();toast('Bounding box updated.');}catch(error){toast(error.message,true)}finally{state.busy=false;}return;}
  $('report-page').textContent=state.page;$('report-description').value='';$('report-error').textContent='';$('report-catalog').innerHTML='<option value="">Unclassified / not in catalog</option>'+state.submission.catalog.map(item=>`<option value="${escapeHTML(item.id)}">${escapeHTML(item.id+' · '+item.description)}</option>`).join('');$('report-dialog').showModal();$('report-description').focus();});
$('report-back').addEventListener('click',()=>{$('report-dialog').close();state.box=null;renderBoxes()});$('report-close').addEventListener('click',()=>{$('report-dialog').close();cancelDraw()});$('report-dialog').addEventListener('cancel',cancelDraw);
$('report-form').addEventListener('submit',async event=>{event.preventDefault();event.submitter.disabled=true;$('report-error').textContent='';try{const result=await api('/api/action',{action:'self_report',submission:state.submission.id,page:state.page,description:$('report-description').value.trim(),catalog_id:$('report-catalog').value,box:state.box});$('report-dialog').close();cancelDraw();state.selected=result.id;await loadSubmission();toast('Self-report submitted for instructor review.');}catch(error){$('report-error').textContent=error.message;}finally{event.submitter.disabled=false;}});
$('refresh').addEventListener('click',async()=>{if(hasDraft())return toast('Submit or cancel your draft before refreshing.',true);try{await loadSubmission(undefined,true);toast('Feedback refreshed.')}catch(error){toast(error.message,true)}});
$('import-data').addEventListener('click',async()=>{if(hasDraft())return toast('Submit or cancel your draft before importing.',true);try{const result=await api('/api/refresh-imports',{});await loadSubmissions(state.submission?.id);importNotice(result)}catch(error){toast(error.message,true)}});
$('scoring-info').addEventListener('click',()=>$('rules-dialog').showModal());$('rules-close').addEventListener('click',()=>$('rules-dialog').close());
function renderCatalog() {
  const catalog = [...(state.submission?.catalog || [])].sort((a, b) => a.id.localeCompare(b.id, undefined, {numeric:true}));
  const query = $('catalog-search').value.trim().toLocaleLowerCase();
  const matches = catalog.filter(item => [item.id, item.description, item.theme, item.example].some(value => String(value || '').toLocaleLowerCase().includes(query)));
  $('catalog-count').textContent = query ? `${matches.length} of ${catalog.length} mistakes` : `${catalog.length} mistakes`;
  $('catalog-cards').innerHTML = matches.length ? matches.map(item => `<article class="catalog-card"><div class="catalog-card-top"><span class="catalog-id">${escapeHTML(item.id)}</span>${item.theme ? `<span class="catalog-theme">${escapeHTML(item.theme)}</span>` : ''}</div><h3>${escapeHTML(item.description)}</h3><div class="catalog-example"><p class="eyebrow">EXAMPLE</p><p>${escapeHTML(item.example || 'No example provided in the catalog.')}</p></div></article>`).join('') : '<p class="empty catalog-empty">'+(catalog.length ? 'No mistakes match your search.' : 'No catalog is available for this submission.')+'</p>';
}
$('catalog-info').addEventListener('click', () => {
  $('catalog-search').value = '';
  renderCatalog();
  $('catalog-dialog').showModal();
  $('catalog-cards').scrollTop = 0;
  $('catalog-search').focus();
});
$('catalog-close').addEventListener('click', () => $('catalog-dialog').close());
$('catalog-search').addEventListener('input', () => { renderCatalog(); $('catalog-cards').scrollTop = 0; });
// Keep shared instructor decisions current without replacing a draft in progress.
setInterval(async()=>{if(!state.user || state.busy || document.hidden)return;if(state.user.role==='student'&&!state.submission){try{await loadSubmissions()}catch{}return;}if(!state.submission||hasDraft())return;try{const value=await api('/api/submission?submission='+encodeURIComponent(state.submission.id));if(value.id===state.submission.id&&JSON.stringify(value)!==JSON.stringify(state.submission)&&!hasDraft()&&!state.busy){state.submission=value;renderHeader();renderFindings();renderBoxes();}}catch(error){if(error.status===404&&state.user?.role==='student'){await loadSubmissions();toast('Your available evaluations have changed.');}}},15000);
(async()=>{setRole('student');try{state.user=await api('/api/me');await enterWorkspace()}catch{showLogin()}})();

// Instructor credentials reference for manual email delivery.
const credentialState={students:[],edits:new Map(),busy:false};
function credentialControls(){
  const dirty=credentialState.edits.size>0;
  $('credentials-save').disabled=credentialState.busy||!dirty;
  $('credentials-discard').disabled=credentialState.busy||!dirty;
  $('credentials-upload').disabled=credentialState.busy;
  $('credentials-download').setAttribute('aria-disabled',credentialState.busy||dirty?'true':'false');
  $('credentials-summary').textContent=`${credentialState.students.length} students${dirty?' · '+credentialState.edits.size+' unsaved roster changes':''}`;
}
function renderCredentials(){
  const show=$('credentials-show-keys').checked;
  $('credentials-rows').innerHTML=credentialState.students.map(student=>{
    const value=credentialState.edits.get(student.identifier)||student;
    return `<tr data-student="${escapeHTML(student.identifier)}"><td data-label="Student identifier"><strong>${escapeHTML(student.identifier)}</strong><small>${student.pdfs.map(escapeHTML).join('<br>')}</small></td><td data-label="Roster name"><input aria-label="Roster name for ${escapeHTML(student.identifier)}" data-credential-field="name" value="${escapeHTML(value.name)}" maxlength="160" placeholder="Roster name" autocomplete="off"></td><td data-label="Student email"><input aria-label="Email for ${escapeHTML(student.identifier)}" data-credential-field="email" value="${escapeHTML(value.email)}" type="email" maxlength="254" placeholder="student@wisc.edu" autocomplete="off"></td><td data-label="Login key"><code>${escapeHTML(student.key?(show?student.key:'••••••••••••••••••••'):'Not available')}</code></td></tr>`;
  }).join('')||'<tr><td colspan="4" class="empty">Import evaluated PDFs to create student credentials.</td></tr>';
  credentialControls();
}
async function loadCredentials(){
  const data=await api('/api/credentials');
  if(!state.user||state.user.role!=='instructor'||!$('credentials-dialog').open)return;
  credentialState.students=data.students;credentialState.edits.clear();renderCredentials();
}
$('student-access').addEventListener('click',async()=>{
  $('credentials-error').textContent='';$('credentials-summary').textContent='Loading credentials…';$('credentials-dialog').showModal();
  try{await loadCredentials()}catch(error){$('credentials-error').textContent=error.message;}
});
function closeCredentials(event){
  if(credentialState.edits.size){event?.preventDefault();$('credentials-error').textContent='Save or discard roster changes before closing.';return;}
  $('credentials-dialog').close();
}
$('credentials-close').addEventListener('click',closeCredentials);
$('credentials-dialog').addEventListener('cancel',event=>{if(credentialState.edits.size){event.preventDefault();$('credentials-error').textContent='Save or discard roster changes before closing.';}});
$('credentials-dialog').addEventListener('close',()=>{credentialState.students=[];credentialState.edits.clear();$('credentials-rows').replaceChildren();$('credentials-show-keys').checked=false;});
$('credentials-show-keys').addEventListener('change',renderCredentials);
$('credentials-rows').addEventListener('input',event=>{
  const field=event.target.dataset.credentialField;if(!field)return;
  const identifier=event.target.closest('[data-student]').dataset.student;
  const student=credentialState.students.find(item=>item.identifier===identifier);
  const value={...student,...credentialState.edits.get(identifier),[field]:event.target.value};
  credentialState.edits.set(identifier,value);credentialControls();
});
$('credentials-upload').addEventListener('click',()=>$('credentials-file').click());
$('credentials-file').addEventListener('change',async event=>{
  const file=event.target.files[0];if(!file)return;$('credentials-error').textContent='';
  try{
    if(file.size>1_000_000)throw new Error('Choose a roster CSV smaller than 1 MB.');
    credentialState.busy=true;credentialControls();
    const preview=await api('/api/access/roster-preview',{csv:await file.text()});
    if(preview.errors.length)throw new Error(preview.errors.join('\n'));
    for(const row of preview.rows)credentialState.edits.set(row.identifier,row);
    renderCredentials();
  }catch(error){$('credentials-error').textContent=error.message;}finally{credentialState.busy=false;event.target.value='';credentialControls();}
});
$('credentials-save').addEventListener('click',async()=>{
  $('credentials-error').textContent='';
  try{
    credentialState.busy=true;credentialControls();
    await api('/api/access/roster-save',{rows:[...credentialState.edits.values()].map(row=>({identifier:row.identifier,name:row.name,email:row.email,version:row.version}))});
    await loadCredentials();toast('Student names and emails saved.');
  }catch(error){$('credentials-error').textContent=error.message;}finally{credentialState.busy=false;credentialControls();}
});
$('credentials-discard').addEventListener('click',()=>{credentialState.edits.clear();$('credentials-error').textContent='';renderCredentials();});
$('credentials-download').addEventListener('click',event=>{if(credentialState.busy||credentialState.edits.size){event.preventDefault();$('credentials-error').textContent='Save or discard roster changes before downloading credentials.';}});
function clearStudentAccess(){
  credentialState.students=[];credentialState.edits.clear();$('credentials-rows').replaceChildren();$('credentials-show-keys').checked=false;
  if($('credentials-dialog').open)$('credentials-dialog').close();
}

function importNotice(result){
  const message=result.imported?result.imported+' submissions imported.':'This evaluation is already available; saved reviews were kept.';
  if(result.errors?.length)toast(message+' Import issue: '+result.errors.map(item=>item.folder+': '+item.error).join('; '),true);
  else toast(message);
}
$('upload-evaluation').addEventListener('click',()=>{
  if(hasDraft())return toast('Submit or cancel your draft before importing.',true);
  $('evaluation-file').click();
});
$('evaluation-file').addEventListener('change',async event=>{
  const file=event.target.files[0];if(!file)return;
  const button=$('upload-evaluation');
  if(file.size>250*1024*1024){event.target.value='';return toast('Choose an evaluation ZIP no larger than 250 MB.',true);}
  button.disabled=true;button.textContent='Importing evaluation…';
  try{
    const response=await fetch('/api/import-run',{method:'POST',credentials:'same-origin',headers:{'Content-Type':'application/zip','X-CSRF-Token':state.user.csrf,'X-File-Name':encodeURIComponent(file.name)},body:file});
    const result=await response.json();
    if(!response.ok)throw new Error(result.error||'Could not import the evaluation.');
    await loadSubmissions(state.submission?.id);importNotice(result);
  }catch(error){toast(error.message,true)}finally{button.disabled=false;button.textContent='＋ Import evaluation ZIP';event.target.value='';}
});

// Instructor control over student access to complete evaluation batches.
let batchRows=[],deletePreview=null,deleteBusy=false;
async function loadBatches(){
  const [rows,storage]=await Promise.all([api('/api/batches'),api('/api/batches/storage')]);batchRows=rows;$('batch-cleanup').hidden=!storage.pending;$('batch-storage-status').textContent=storage.pending?'Some deleted files are awaiting cleanup. Use Finish pending deletions to free the remaining storage.':'';
  $('batch-list').innerHTML=batchRows.length?batchRows.map(item=>`<article class="batch-card"><div><h3>${escapeHTML(item.label)}</h3><p class="muted">${item.students} student submission(s) · ${escapeHTML(new Date(item.imported*1000).toLocaleString())}</p><p>${item.visible?'Visible to students':'Hidden from students · saved for instructors'}</p></div><label><input type="checkbox" data-batch="${escapeHTML(item.id)}" ${item.visible?'checked':''}> Visible to students</label><button class="delete-batch-button" data-delete-batch="${escapeHTML(item.id)}" type="button">Delete batch…</button></article>`).join(''):'<p class="empty">Import an evaluation ZIP to add a batch.</p>';
}
$('manage-batches').addEventListener('click',async()=>{
  $('batches-error').textContent='';$('batch-list').replaceChildren();$('batches-dialog').showModal();
  try{await loadBatches()}catch(error){$('batches-error').textContent=error.message;}
});
$('batches-close').addEventListener('click',()=>$('batches-dialog').close());
$('batches-refresh').addEventListener('click',async()=>{try{await loadBatches();$('batches-error').textContent='';}catch(error){$('batches-error').textContent=error.message;}});
$('batch-list').addEventListener('change',async event=>{
  const input=event.target.closest('[data-batch]');if(!input)return;
  const row=batchRows.find(item=>item.id===input.dataset.batch);input.disabled=true;$('batches-error').textContent='';
  try{await api('/api/batches/visibility',{assignment:row.id,visible:input.checked,version:row.version});await loadBatches();toast(input.checked?'Batch is visible to students.':'Batch hidden from students. Saved reviews are preserved.');}
  catch(error){input.checked=row.visible;input.disabled=false;$('batches-error').textContent=error.message;}
});

function storageSize(bytes){return bytes>=1024*1024?`${(bytes/1024/1024).toFixed(1)} MB`:`${Math.ceil(bytes/1024)} KB`;}
$('batch-list').addEventListener('click',async event=>{
  const button=event.target.closest('[data-delete-batch]');if(!button)return;
  if(hasDraft())return toast('Save or cancel your feedback draft before deleting a batch.',true);
  button.disabled=true;$('batches-error').textContent='';
  try{
    deletePreview=await api('/api/batches/delete-preview?assignment='+encodeURIComponent(button.dataset.deleteBatch));
    $('delete-batch-name').textContent=deletePreview.label;
    $('delete-batch-summary').textContent=`${deletePreview.students} student submissions · ${deletePreview.findings} findings · approximately ${storageSize(deletePreview.bytes)} across ${deletePreview.copies} stored copies will be removed.`;
    $('delete-batch-confirmation').value='';$('delete-batch-submit').disabled=true;$('delete-batch-error').textContent='';
    $('delete-batch-dialog').showModal();$('delete-batch-confirmation').focus();
  }catch(error){$('batches-error').textContent=error.message;}finally{button.disabled=false;}
});
$('delete-batch-confirmation').addEventListener('input',()=>{$('delete-batch-submit').disabled=deleteBusy||$('delete-batch-confirmation').value!=='DELETE';});
$('delete-batch-cancel').addEventListener('click',()=>{if(!deleteBusy){$('delete-batch-dialog').close();deletePreview=null;}});
$('delete-batch-dialog').addEventListener('cancel',event=>{if(deleteBusy)event.preventDefault();});
$('delete-batch-form').addEventListener('submit',async event=>{
  event.preventDefault();if(deleteBusy||!deletePreview||$('delete-batch-confirmation').value!=='DELETE')return;
  deleteBusy=true;$('delete-batch-submit').disabled=true;$('delete-batch-cancel').disabled=true;$('delete-batch-error').textContent='';
  try{
    const result=await api('/api/batches/delete',{assignment:deletePreview.assignment,token:deletePreview.token,confirmation:'DELETE'});
    $('delete-batch-dialog').close();deletePreview=null;
    await loadBatches();await loadSubmissions(state.submission?.id);
    toast(result.cleanup_pending?'Batch deleted. Some files are busy; use Finish pending deletions to complete storage cleanup.':`Batch deleted. Approximately ${storageSize(result.bytes)} of batch files removed.`,result.cleanup_pending);
  }catch(error){$('delete-batch-error').textContent=error.message;}
  finally{deleteBusy=false;$('delete-batch-submit').disabled=$('delete-batch-confirmation').value!=='DELETE';$('delete-batch-cancel').disabled=false;}
});
$('batch-cleanup').addEventListener('click',async()=>{
  $('batch-cleanup').disabled=true;
  try{const result=await api('/api/batches/cleanup',{});await loadBatches();toast(result.pending?'Some files are still busy. Try cleanup again shortly.':'Pending deletions completed.',!!result.pending);}
  catch(error){$('batches-error').textContent=error.message;}
  finally{$('batch-cleanup').disabled=false;}
});
