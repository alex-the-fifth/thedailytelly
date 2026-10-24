/* No analytics, external scripts, local identifiers, or browser fingerprinting. */
'use strict';
const $ = selector => document.querySelector(selector);
const days = ['Monday','Tuesday','Wednesday','Thursday','Friday','Saturday','Sunday'];
const allowedNext = ['/app','/activate','/owner','/kid/'];
let account;
function saveNext(next) {
  if(!allowedNext.includes(next))return;
  const value=JSON.stringify({path:next,at:Date.now()});
  try { localStorage.setItem('telly-next',value); }
  catch { try { sessionStorage.setItem('telly-next',value); } catch {} }
}
function takeNext() {
  let value;
  try { value=localStorage.getItem('telly-next');if(value)localStorage.removeItem('telly-next'); } catch {}
  if(!value)try { value=sessionStorage.getItem('telly-next');sessionStorage.removeItem('telly-next'); } catch {}
  try {
    const pending=JSON.parse(value);
    return Date.now()-pending.at < 60*60*1000 && allowedNext.includes(pending.path) ? pending.path : null;
  } catch { return null; }
}
function el(tag, text, className) { const node=document.createElement(tag); if(text!==undefined)node.textContent=text; if(className)node.className=className; return node; }
function message(text, error=false) { const node=$('#message'); if(!node)return; node.hidden=false; node.className='message'+(error?' error':''); node.textContent=text; }
async function api(path, options={}) {
  const response=await fetch(path,{credentials:'same-origin',...options,headers:{'Content-Type':'application/json',...options.headers}});
  const data=await response.json().catch(()=>({}));
  if(!response.ok) { const error=new Error(typeof data.detail==='string'?data.detail:'Please check the details and try again.'); error.status=response.status; throw error; }
  return data;
}
const post=(path,body={})=>api(path,{method:'POST',body:JSON.stringify(body)});
async function action(task,button) { if(button)button.disabled=true; try{return await task();}catch(error){message(error.message,true);}finally{if(button)button.disabled=false;} }
function form(id,task) { $(id)?.addEventListener('submit',event=>{event.preventDefault();action(task,event.target.querySelector('button'));}); }
function button(text,task,danger=false) { const node=el('button',text,'small'+(danger?' danger':'')); node.type='button';node.addEventListener('click',()=>action(task,node));return node; }
async function signedIn() {
  try { account=await api('/auth/me'); return true; }
  catch(error) { if(error.status===401){location.assign('/login?next='+encodeURIComponent(location.pathname));return false;} throw error; }
}
function date(seconds) { return seconds?new Date(seconds*1000).toLocaleString():'Not yet'; }

async function start() {
  const page=document.body.dataset.page;
  if(page==='email-preferences') {
    const token=location.hash.slice(1);history.replaceState(null,'','/email-preferences');
    if(!token){message('Open the preference link from your welcome email, or use account settings.',true);$('#skip-reminder').disabled=true;return;}
    $('#skip-reminder').onclick=()=>action(async()=>{const result=await post('/email-preferences',{token});message(result.message);$('#skip-reminder').hidden=true;},$('#skip-reminder'));
  }
  if(page==='contact') {
    form('#contact-form',async()=>{
      const result=await post('/contact',{email:$('#contact-email').value,topic:$('#contact-topic').value,
        message:$('#contact-message').value,website:$('#contact-website').value});
      message(result.message);$('#contact-form').hidden=true;$('#message').scrollIntoView({block:'nearest'});
    });
  }
  if(page==='login') {
    const next=new URLSearchParams(location.search).get('next');
    saveNext(next);
    try {
      account=await api('/auth/me');
      location.replace(allowedNext.includes(next)?next:'/app');
      return;
    } catch {}
    $('#email-form').hidden=true;
    const mode=await api('/auth/mode');
    if(!mode.email_enabled){
      $('#login-intro').textContent='We are preparing public email sign-in. Registration is not open yet.';
      $('#login-footnote').textContent='Existing platform owners can use their private server-issued sign-in link.';
      return;
    }
    $('#email-form').hidden=false;
    form('#email-form',async()=>{
      const result=await post('/auth/email',{email:$('#email').value,accepted:$('#accepted').checked,reminder:$('#reminder-optin').checked});
      message(result.message); $('#email-form').hidden=true;
    });
  }
  if(page==='owner-access') {
    const token=location.hash.slice(1);history.replaceState(null,'','/owner-access');
    if(!token){message('Open the private owner link issued on your server.',true);$('#owner-submit').disabled=true;return;}
    form('#owner-form',async()=>{await post('/auth/operator',{token,accepted:$('#accepted').checked});location.assign('/app');});
  }
  if(page==='verify') {
    const token=location.hash.slice(1); history.replaceState(null,'','/verify');
    if(!token){message('Request a fresh sign-in email, then open its link in this browser.',true);$('#verify').disabled=true;return;}
    $('#verify').addEventListener('click',()=>action(async()=>{
      await post('/auth/verify',{token});
      const next=takeNext();
      location.assign(allowedNext.includes(next)?next:'/app');
    },$('#verify')));
  }
  if(page==='activate') {
    if(!await signedIn())return;
    $('#signed-email').textContent='Signed in as '+account.email+'. Only link a device you can see.';
    form('#pair-form',async()=>{
      const data=await post('/pair/approve',{code:$('#code').value,confirm:$('#confirm').checked});
      message(data.label+' is approved. Your TV will finish linking in a moment.');$('#pair-form').hidden=true;
    });
  }
  if(page==='dashboard') {
    if(!await signedIn())return;
    $('#owner-link').hidden=!account.is_owner;
    $('#account-email').textContent=account.email+' · Adult-managed account';
    $('#logout').onclick=()=>action(async()=>{await post('/auth/logout');location.assign('/');});
    if(location.pathname==='/owner') {
      $('#sidebar').parentElement.classList.add('owner-shell');
      if(!account.is_owner){message('This page is only available to the platform owner.',true);$('#sidebar').hidden=true;return;}
      document.querySelectorAll('[data-panel]').forEach(node=>node.hidden=true);
      $('#sidebar').hidden=true;$('#ops-panel').hidden=false;$('#view-title').textContent='A quiet watch on the service.';
      $('#view-description').textContent='Aggregate server health and account administration. No family viewing feed.';
      await ops();$('#metrics-refresh').onclick=()=>action(ops,$('#metrics-refresh'));
      await starterDefaults();form('#starter-defaults',async()=>{
        const entries=days.map((_,weekday)=>({weekday,channel_id:$('#starter-day-'+weekday).value})).filter(row=>row.channel_id);
        if(!entries.length)throw new Error('Choose at least one starter channel.');
        const result=await api('/ops/starter',{method:'PUT',body:JSON.stringify({enabled:$('#starter-enabled').checked,entries})});
        $('#starter-status').textContent='Published for future accounts: '+result.channels+' day/channel entries and '+result.videos.toLocaleString()+' cached videos.';
      });
      setInterval(()=>opsMetrics().catch(()=>{}),60000);
      return;
    }
    $('#sidebar').addEventListener('click',event=>{if(event.target.dataset.view)action(()=>view(event.target.dataset.view));});
    form('#channel-form',async()=>{await api('/admin/channels',{method:'PUT',body:JSON.stringify({weekday:Number($('#weekday').value),query:$('#channel').value})});$('#channel').value='';await loadSchedule();message('Channel added. Its videos will appear after the catalog refresh.');});
    $('#sync').onclick=()=>action(async()=>{await post('/admin/sync');message('Refreshing your catalog. You can stay here while it finishes.');},$('#sync'));
    form('#settings-form',async()=>{await api('/admin/settings',{method:'PUT',body:JSON.stringify({min_seconds:Math.round(Number($('#min').value)*60),max_seconds:Math.round(Number($('#max').value)*60),timezone:$('#timezone').value,sync_interval_minutes:Number($('#interval').value)})});message('Your viewing rules are saved.');});
    form('#keyword-form',async()=>{await post('/admin/keywords',{keyword:$('#keyword').value});$('#keyword').value='';await keywords();});
    $('#delete-account').onclick=()=>action(async()=>{
      await post('/account/delete',{confirmation:$('#delete-confirm').value});location.assign('/');
    },$('#delete-account'));
    await view('schedule');
    const setup=await api('/account/setup');$('#starter-tour').hidden=!setup.show_tour;
    $('#tour-dismiss').onclick=()=>action(async()=>{await post('/account/setup/seen');$('#starter-tour').hidden=true;},$('#tour-dismiss'));
    $('#welcome-preference').checked=setup.reminder_enabled;
    $('#welcome-preference').disabled=!setup.reminder_available;
    $('#reminder-state').textContent=setup.reminder?.sent_at?'Your one welcome check-in has already been sent.':setup.reminder?.due_at?'Scheduled for '+date(setup.reminder.due_at)+', if enabled.':'No welcome check-in is pending.';
    $('#welcome-preference').onchange=()=>action(async()=>{const enabled=$('#welcome-preference').checked;try{await api('/account/reminder',{method:'PATCH',body:JSON.stringify({enabled})});message('Your email preference is saved.');}catch(error){$('#welcome-preference').checked=!enabled;throw error;}});
    setInterval(async()=>{try{const status=await api('/admin/sync-status');$('#sync-status').textContent=status.running?'Refreshing your catalog…':status.last_finished_at?'Last catalog refresh: '+new Date(status.last_finished_at).toLocaleString():'';}catch{}},10000);
  }
}

const titles={community:['A little community.','Ideas, questions, and improvements — shared in the open.'],schedule:['Your weekly rhythm.','A few good channels. A little shape to the week.'],library:['Your library.','Review the catalog and keep only what fits your family.'],history:['A little look back.','Your household’s saved progress. This is private to your account.'],settings:['Your viewing rules.','Choose the lengths, timezone, and title filters that work for you.'],devices:['Your linked screens.','Every device has its own revocable, viewing-only credential.'],account:['Your account.','Minimal details. Clear choices. Your data belongs to you.']};
async function view(name) {
  if(!titles[name])return;
  document.querySelectorAll('[data-panel]').forEach(panel=>panel.hidden=panel.dataset.panel!==name);
  document.querySelectorAll('[data-view]').forEach(tab=>tab.classList.toggle('active',tab.dataset.view===name));
  $('#view-title').textContent=titles[name][0];$('#view-description').textContent=titles[name][1];$('#message').hidden=true;
  if(name==='schedule')await loadSchedule();
  if(name==='library')await loadLibrary();
  if(name==='history')await loadHistory();
  if(name==='devices')await loadDevices();
  if(name==='settings') {
    const settings=await api('/admin/settings');$('#min').value=settings.min_seconds/60;$('#max').value=settings.max_seconds/60;$('#interval').value=settings.sync_interval_minutes;$('#timezone').value=settings.timezone;await keywords();
  }
}
async function loadSchedule() {
  const data=await api('/admin/channels');const host=$('#schedule');host.replaceChildren();
  days.forEach((day,index)=>{
    const row=el('div',undefined,'day-row');row.append(el('strong',day));const chips=el('div',undefined,'chips');
    const channels=data.channels.filter(channel=>channel.weekday===index);
    if(!channels.length)chips.append(el('span','A little room for something good.','fine'));
    channels.forEach(channel=>{const chip=el('span',undefined,'chip');chip.append(el('span',channel.title));const remove=button('×',async()=>{await api('/admin/channels/'+index+'/'+encodeURIComponent(channel.channel_id),{method:'DELETE'});await loadSchedule();});remove.setAttribute('aria-label','Remove '+channel.title+' from '+day);chip.append(remove);chips.append(chip);});
    row.append(chips);host.append(row);
  });
}
function videoRow(video) {
  const row=el('div',undefined,'video-row');
  if(video.thumbnail_url){const img=el('img');img.src=video.thumbnail_url;img.alt='';img.loading='lazy';row.append(img);}
  const details=el('div',undefined,'details');details.append(el('h3',video.title));details.append(el('p',Math.floor(video.duration_seconds/60)+' min · '+(video.reason||'Saved progress')));row.append(details);return row;
}
async function loadLibrary() {
  const data=await api('/admin/videos');const host=$('#library');host.replaceChildren();
  if(!data.videos.length)host.append(el('p','Your catalog is quiet for now. Add channels to the schedule, then let the catalog refresh.','empty'));
  data.videos.forEach(video=>{const row=videoRow(video);row.append(button(video.blacklisted?'Unblock':'Block',async()=>{await post('/admin/videos/'+encodeURIComponent(video.video_id)+'/blacklist');await loadLibrary();}));if(video.completed)row.append(button('Reset progress',async()=>{await post('/admin/videos/'+encodeURIComponent(video.video_id)+'/reset-progress');await loadLibrary();}));host.append(row);});
}
async function loadHistory() {
  const data=await api('/admin/history');const host=$('#history');host.replaceChildren();
  if(!data.history.length)host.append(el('p','Nothing watched yet. Your saved progress will appear here.','empty'));
  data.history.forEach(video=>{const row=videoRow(video);row.append(button('Reset',async()=>{await post('/admin/videos/'+encodeURIComponent(video.video_id)+'/reset-progress');await loadHistory();}));host.append(row);});
}
async function keywords() {
  const data=await api('/admin/keywords');const host=$('#keywords');host.replaceChildren();
  data.keywords.forEach(keyword=>{const row=el('div',undefined,'day-row');row.append(el('span',keyword.keyword));row.append(button('Remove',async()=>{await api('/admin/keywords/'+encodeURIComponent(keyword.keyword),{method:'DELETE'});await keywords();}));host.append(row);});
}
async function loadDevices() {
  const data=await api('/account/devices');const host=$('#devices');host.replaceChildren();
  if(!data.devices.length)host.append(el('p','No linked TVs yet. Your browser can watch today’s schedule while signed in.','empty'));
  data.devices.forEach(device=>{const row=el('div',undefined,'day-row');row.append(el('span',device.label+' · linked '+date(device.created_at)));row.append(button('Unlink',async()=>{await api('/account/devices/'+device.id,{method:'DELETE'});await loadDevices();},true));host.append(row);});
}
async function opsMetrics() {
  const data=await api('/ops/metrics');const host=$('#metrics');host.replaceChildren();
  for(const [key,label] of [['cpu','CPU'],['memory','Memory'],['disk','Disk']]) {const metric=el('div',undefined,'metric');metric.append(el('span',label),el('b',data.current[key].toFixed(1)+'%'));const meter=el('div',undefined,'meter');const fill=el('i');fill.style.width=Math.min(100,data.current[key])+'%';meter.append(fill);metric.append(meter);host.append(metric);}
  const metric=el('div',undefined,'metric');metric.append(el('span','Process memory'),el('b',data.current.process_mb+' MB'));host.append(metric);
  $('#health-notes').textContent='Uptime '+Math.floor(data.current.uptime_seconds/60)+' min · '+data.current.requests+' requests · '+data.current.errors+' server errors. Pushover '+(data.current.pushover_configured?'configured':'not configured')+'. Resource scope: '+(data.current.scope||'host')+'. Alerts trigger after 3 sustained high samples, with a 30-minute cooldown. Server metrics are retained for 24 hours. Catalog reservations: '+(data.catalog_quota||[]).map(q=>q.bucket+' '+q.units).join(', ');
  $('#health-notes').textContent+=' Welcome check-ins: '+Object.entries(data.welcome_reminders||{}).map(([state,count])=>state+' '+count).join(', ')+'.';
  const audit=$('#ops-audit');audit.replaceChildren();
  data.audit.forEach(item=>audit.append(el('p',date(item.at)+' · '+item.action+' · account '+item.target_id.slice(0,8))));
  if(!data.audit.length)audit.append(el('p','No operator actions yet.'));
}
let accountRows=[],accountOffset=0,accountsTotal=0,hasMoreAccounts=false;
async function starterDefaults() {
  const data=await api('/ops/starter');$('#starter-enabled').checked=data.enabled;
  const host=$('#starter-entries');host.replaceChildren();
  days.forEach((day,index)=>{
    const row=el('div');const label=el('label',day);label.htmlFor='starter-day-'+index;
    const select=el('select');select.id=label.htmlFor;select.append(new Option('Leave this day open',''));
    data.choices.forEach(choice=>select.append(new Option(choice.title,choice.channel_id)));
    select.value=data.entries.find(entry=>entry.weekday===index)?.channel_id||'';
    row.append(label,select);host.append(row);
  });
  $('#starter-status').textContent=data.entries.length+' day/channel entries · '+data.videos.toLocaleString()+' cached videos. Publish to refresh the template from your library.';
}
function renderAccounts() {
  const host=$('#accounts');host.replaceChildren();const table=el('table');const header=el('tr');['Email','Status','Verified','Last login','Devices','Action'].forEach(text=>header.append(el('th',text)));table.append(header);
  const search=$('#account-search').value.toLowerCase();
  accountRows.filter(row=>row.email.includes(search)).forEach(row=>{const tr=el('tr');[row.email,row.status,date(row.verified_at),date(row.last_login_at),String(row.devices)].forEach(text=>tr.append(el('td',text)));const cell=el('td');if(row.verified_at&&row.email!==account.email)cell.append(button(row.status==='suspended'?'Restore':'Suspend',async()=>{await api('/ops/accounts/'+row.id,{method:'PATCH',body:JSON.stringify({status:row.status==='suspended'?'active':'suspended'})});await ops();},row.status!=='suspended'));tr.append(cell);table.append(tr);});host.append(table);const pager=el("div",undefined,"actions");if(accountOffset>0)pager.append(button("Previous",async()=>{accountOffset=Math.max(0,accountOffset-100);await loadAccounts();}));if(hasMoreAccounts)pager.append(button("Next",async()=>{accountOffset+=100;await loadAccounts();}));pager.append(el("span",accountsTotal+" matching accounts","fine"));host.append(pager);
}
async function loadAccounts(){const data=await api('/ops/accounts?offset='+accountOffset+'&q='+encodeURIComponent($('#account-search').value));accountRows=data.accounts;accountsTotal=data.total;hasMoreAccounts=data.has_more;renderAccounts();}
async function ops(){await opsMetrics();await loadAccounts();let searchTimer;$('#account-search').oninput=()=>{clearTimeout(searchTimer);searchTimer=setTimeout(()=>{accountOffset=0;action(loadAccounts);},300);};}
start().catch(error=>message(error.message,true));
