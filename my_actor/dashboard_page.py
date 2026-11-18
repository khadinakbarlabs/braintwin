"""The dashboard page (one file, no external scripts, fonts or images).

Every piece of memory text is put on the page with `textContent` only, never as HTML, so a stored
note cannot inject markup or script. The CSP nonce is added per response by dashboard.py.
"""

from __future__ import annotations

from .dashboard_sources import SOURCES_CSS, SOURCES_JS

_BASE = r"""<!doctype html>
<html lang="en">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<meta name="robots" content="noindex">
<title>Braintwin</title>
<meta name="theme-color" content="#ffffff" media="(prefers-color-scheme: light)">
<meta name="theme-color" content="#0a0a0a" media="(prefers-color-scheme: dark)">
<link rel="icon" href="data:image/svg+xml,%3Csvg xmlns='http://www.w3.org/2000/svg' viewBox='0 0 32 32'%3E%3Crect width='32' height='32' rx='8' fill='%230a0a0a'/%3E%3Ccircle cx='13' cy='16' r='6.5' fill='%23ffffff'/%3E%3Ccircle cx='20' cy='16' r='5.6' fill='none' stroke='%23ffffff' stroke-width='1.9'/%3E%3C/svg%3E">
<style nonce="__NONCE__">
:root{--bg:#fff;--card:#fff;--soft:#f5f5f5;--ink:#0a0a0a;--mute:#666;--line:#eaeaea;--acc:#0a0a0a;--acc-ink:#fff;--tint:#f5f5f5;--warn:#b4441a;--warn-tint:#fdf0e9;--shadow:none;--r:10px;--glow:transparent;--serif:ui-sans-serif,system-ui,-apple-system,'Segoe UI',Roboto,sans-serif}
@media (prefers-color-scheme:dark){:root{--bg:#0a0a0a;--card:#0f0f0f;--soft:#171717;--ink:#ededed;--mute:#a0a0a0;--line:#262626;--acc:#ededed;--acc-ink:#0a0a0a;--tint:#171717;--warn:#f0936a;--warn-tint:#2e170d;--shadow:none;--glow:transparent}}
*{box-sizing:border-box}
html,body{margin:0}
body{background:var(--bg);color:var(--ink);font:14.5px/1.55 system-ui,-apple-system,'Segoe UI',Roboto,sans-serif;min-height:100vh;-webkit-font-smoothing:antialiased;letter-spacing:-.003em}
::selection{background:var(--acc);color:var(--acc-ink)}
button,input,select,textarea{font:inherit;color:inherit}
.shell{max-width:760px;margin:0 auto;padding:0 24px 96px}
header.top{position:sticky;top:0;z-index:5;background:color-mix(in srgb,var(--bg) 82%,transparent);backdrop-filter:saturate(180%) blur(14px);display:flex;align-items:center;gap:14px;padding:12px 0;margin-bottom:40px;border-bottom:1px solid var(--line);flex-wrap:wrap}
.brand{display:flex;align-items:center;gap:16px;font-weight:700;letter-spacing:-.015em;margin-right:auto;font-size:17px}
.dot{width:13px;height:13px;border-radius:50%;background:var(--acc);position:relative}
.dot::after{content:'';position:absolute;inset:0;left:8px;width:13px;border-radius:50%;border:2px solid var(--acc)}
h1{margin:0;font-size:inherit}
nav.rail{display:flex;gap:2px;background:none;border:0;padding:0;overflow:auto;max-width:100%}
nav.rail button{background:none;border:0;padding:7px 12px;border-radius:8px;cursor:pointer;color:var(--mute);white-space:nowrap;font-weight:550;display:flex;gap:6px;align-items:center;transition:color .15s,background .15s}
nav.rail button:hover{color:var(--ink)}
nav.rail button[aria-current=true]{background:var(--soft);color:var(--ink)}
nav.rail button:focus-visible,.btn:focus-visible,input:focus-visible,select:focus-visible,textarea:focus-visible,summary:focus-visible,.seg button:focus-visible{outline:2px solid var(--acc);outline-offset:2px}
.count{font-size:11px;font-weight:700;background:var(--warn);color:#fff;border-radius:99px;padding:0 7px;line-height:18px}
.count[hidden]{display:none}
.seg{display:flex;gap:6px;flex-wrap:wrap;margin-bottom:18px}
.seg button{background:none;border:1px solid var(--line);border-radius:99px;padding:6px 13px;cursor:pointer;color:var(--mute);font-weight:550}
.seg button[aria-current=true],.seg button[aria-pressed=true]{background:var(--ink);color:var(--bg);border-color:var(--ink)}
h2{font-size:1.05rem;font-weight:650;margin:0 0 6px;letter-spacing:-.01em}
.hero{padding-top:14px}
.hero h2{font:650 clamp(1.8rem,1.2rem + 2.4vw,2.6rem)/1.08 var(--serif);letter-spacing:-.045em;margin:10px 0 10px;text-wrap:balance}
.hero p{color:var(--mute);margin:0 0 22px;max-width:56ch;font-size:15.5px}
.card{background:var(--card);border:1px solid var(--line);border-radius:var(--r);padding:20px;box-shadow:var(--shadow);margin-bottom:14px}
.card>:last-child{margin-bottom:0}
.connect .card>h3,.connect .card>div>h3{font-size:1rem;margin:0 0 10px}
details.card>summary{cursor:pointer;font-weight:650;list-style:none;display:flex;justify-content:space-between;align-items:center;gap:10px}
details.card>summary::-webkit-details-marker{display:none}
details.card>summary::after{content:'+';color:var(--mute);font-weight:400;font-size:20px;line-height:1}
details.card[open]>summary{margin-bottom:12px}
details.card[open]>summary::after{content:'–'}
.search{display:flex;gap:8px;background:var(--card);border:1px solid var(--line);border-radius:10px;padding:5px 5px 5px 16px;box-shadow:var(--shadow);margin-bottom:12px;transition:border-color .15s,box-shadow .15s}
.search:focus-within{border-color:var(--acc);box-shadow:0 0 0 4px var(--tint)}
.search input:focus-visible{outline:0}
.search input{flex:1;border:0;background:none;outline:0;min-width:0;padding:8px 0}
.stats{display:flex;gap:10px;flex-wrap:wrap;margin:18px 0}
.stat{position:relative;background:var(--card);border:1px solid var(--line);border-radius:var(--r);padding:14px 16px;flex:1;min-width:120px;cursor:pointer;text-align:left;transition:border-color .15s,translate .15s,box-shadow .15s}
.stat::after{content:'\2192';position:absolute;top:12px;right:14px;color:var(--acc);opacity:0;translate:-4px 0;transition:opacity .15s,translate .15s}
.stat:hover,.stat:focus-visible{border-color:var(--acc)}
.stat:hover::after,.stat:focus-visible::after{opacity:1;translate:0 0}
.stat:focus-visible{outline:2px solid var(--acc);outline-offset:2px}
.stat b{display:block;font:600 1.75rem/1.1 var(--serif);letter-spacing:-.03em;color:var(--ink);font-variant-numeric:tabular-nums}
.stat span{color:var(--mute);font-size:13px}
.bar{display:flex;gap:8px;flex-wrap:wrap;align-items:center;margin-bottom:14px}
.bar input[type=search],.bar select,form.add input,form.add textarea,form.add select,select,input[type=password],input[type=url]{background:var(--card);border:1px solid var(--line);border-radius:10px;padding:9px 12px}
.bar input[type=search]{flex:1;min-width:180px}
.bar input,.bar select,form.add input,form.add textarea,form.add select,select,input[type=password],input[type=url],dialog input{transition:border-color .15s,box-shadow .15s}
.bar input:focus,.bar select:focus,form.add input:focus,form.add textarea:focus,form.add select:focus,select:focus,input[type=password]:focus,input[type=url]:focus,dialog input:focus{border-color:var(--acc);box-shadow:0 0 0 4px var(--tint);outline:0}
.btn{background:var(--ink);color:var(--bg);border:1px solid var(--ink);border-radius:8px;padding:8px 14px;cursor:pointer;font-weight:600;transition:transform .08s,opacity .15s,box-shadow .15s,border-color .15s}
.btn:hover{opacity:.9}.btn:active{transform:scale(.98)}
.btn.acc{background:var(--acc);border-color:var(--acc);color:var(--acc-ink)}
.btn.acc:hover{opacity:.82}
.btn.ghost:hover{border-color:var(--ink);opacity:1}
.btn.ghost{background:none;color:var(--ink);border-color:var(--line)}
.btn.warn{background:none;color:var(--warn);border-color:transparent;padding-inline:8px}
.btn.warn:hover{background:var(--warn-tint)}
.btn.link{background:none;border-color:transparent;color:var(--acc);padding-inline:4px}
.btn:disabled{opacity:.45;cursor:not-allowed}
ul.list{list-style:none;margin:0;padding:0;display:grid;gap:8px}
li.item{background:var(--card);border:1px solid var(--line);border-radius:var(--r);padding:14px 16px;display:grid;gap:5px;transition:border-color .15s}
li.item:hover{border-color:color-mix(in srgb,var(--ink) 30%,var(--line))}
li.item h3{margin:0;font-size:1rem;font-weight:620;display:flex;gap:8px;align-items:center;flex-wrap:wrap;letter-spacing:-.005em}
li.item p{margin:0;color:var(--mute);font-size:14px;overflow-wrap:anywhere}
li.item.done{background:var(--tint);border-color:transparent}
li.item.done h3::before{content:'✓';color:var(--acc);font-weight:800}
ul.steps{counter-reset:s}
ul.steps li.item{counter-increment:s;grid-template-columns:auto 1fr;column-gap:12px;align-items:start}
ul.steps li.item>*{grid-column:2}
ul.steps li.item::before{content:counter(s);grid-column:1;grid-row:1/span 3;width:26px;height:26px;border-radius:50%;display:grid;place-items:center;font-size:12.5px;font-weight:700;border:1.5px solid var(--line);color:var(--mute)}
ul.steps li.item.done::before{content:'✓';background:var(--acc);border-color:var(--acc);color:var(--acc-ink)}
ul.steps li.item.done h3::before{content:none}
ul.steps li.item.done h3{color:var(--mute);font-weight:550}
.cardhead{display:flex;align-items:center;justify-content:space-between;gap:12px;margin-bottom:12px}
.cardhead h2{margin:0}
.meta{display:flex;gap:8px;flex-wrap:wrap;align-items:center;font-size:12.5px;color:var(--mute)}
.tag{border:0;border-radius:99px;padding:1px 9px;cursor:pointer;background:var(--soft);color:var(--mute)}
.tag:hover{color:var(--acc)}
.pill{border-radius:99px;padding:1px 9px;background:var(--soft);color:var(--mute);font-size:12px;font-weight:600}
.pill.proposed{background:var(--warn-tint);color:var(--warn)}.pill.approved{background:var(--tint);color:var(--acc)}
.row{display:flex;gap:8px;align-items:center;flex-wrap:wrap}
.row .grow{flex:1}
form.add{display:grid;gap:12px;max-width:640px}
form.add label{display:grid;gap:5px;font-weight:600;font-size:13.5px}
form.add input,form.add textarea,form.add select{font-weight:400;font-size:15px}
form.add textarea{min-height:130px;resize:vertical}
form.add label.row,label.row{display:flex;font-weight:500;align-items:flex-start;gap:9px;font-size:15px;cursor:pointer}
label.row input{flex:none;margin-top:4px;accent-color:var(--acc);width:16px;height:16px}
.note{color:var(--mute);font-size:14px;margin:0 0 12px}
.kind{border-radius:99px;padding:1px 9px;background:var(--tint);color:var(--acc);font-weight:600}
.use{color:var(--mute);font-size:13.5px;margin:6px 0 0}
.empty{padding:40px 16px;text-align:center;color:var(--mute);border:1px dashed var(--line);border-radius:var(--r);background:color-mix(in srgb,var(--card) 50%,transparent);text-wrap:balance}
.empty::before{content:'';display:block;width:34px;height:22px;margin:0 auto 12px;opacity:.55;background:radial-gradient(circle at 11px 11px,var(--acc) 0 10px,transparent 10.5px),radial-gradient(circle at 23px 11px,transparent 0 8.5px,var(--acc) 9px 10.5px,transparent 11px)}
.banner{display:flex;gap:12px;align-items:center;justify-content:space-between;flex-wrap:wrap;background:var(--warn-tint);color:var(--ink);border-radius:12px;padding:12px 16px;margin-bottom:14px}
pre.snip{white-space:pre-wrap;overflow-wrap:anywhere;font:13px/1.5 ui-monospace,Menlo,monospace;background:var(--soft);border-radius:10px;padding:12px;margin:0 0 10px}
dialog{border:1px solid var(--line);background:var(--card);color:var(--ink);border-radius:12px;width:min(640px,92vw);padding:24px;box-shadow:0 24px 60px #0003}
dialog[open]{animation:pop .22s cubic-bezier(.2,.8,.2,1)}
dialog::backdrop{background:#0007;backdrop-filter:blur(2px)}
dialog pre{white-space:pre-wrap;overflow-wrap:anywhere;font:14.5px/1.6 system-ui,sans-serif;background:var(--soft);border-radius:10px;padding:14px;max-height:50vh;overflow:auto}
dialog input{width:100%;background:var(--card);border:1px solid var(--line);border-radius:10px;padding:9px 12px;margin-bottom:10px}
.toast{position:fixed;left:50%;bottom:22px;translate:-50% 0;animation:toast .25s cubic-bezier(.2,.8,.2,1);background:var(--ink);color:var(--bg);padding:10px 18px;border-radius:99px;font-size:14px;z-index:9;max-width:90vw;box-shadow:0 10px 30px #0004}
.toast.err{background:var(--warn);color:#fff}
.foot{color:var(--mute);font-size:12px;text-align:center;margin-top:36px}
body.out nav.rail{display:none}
body.out .hero{text-align:center;padding:9vh 0 6px;display:grid;justify-items:center}
body.out .hero h2{font-size:clamp(2.2rem,1.3rem + 4vw,3.6rem)}
body.out .hero .btn{padding:12px 24px;font-size:16px}
body.out details.card{max-width:520px;margin:26px auto 0}
.eyebrow{display:inline-flex;align-items:center;gap:8px;font-size:13.5px;font-weight:450;color:var(--mute);border:1px solid var(--line);border-radius:99px;padding:4px 13px}
ul.trust{list-style:none;margin:22px 0 0;padding:0;display:flex;gap:8px 18px;flex-wrap:wrap;justify-content:center;color:var(--mute);font-size:13.5px}
ul.trust li::before{content:'✓';color:var(--acc);font-weight:800;margin-right:6px}
a.btn{text-decoration:none;display:inline-block}
@keyframes rise{from{opacity:0;translate:0 10px}to{opacity:1;translate:0 0}}
@keyframes pop{from{opacity:0;scale:.97}to{opacity:1;scale:1}}
@keyframes toast{from{opacity:0;translate:-50% 10px}to{opacity:1;translate:-50% 0}}
#main.enter>div>*,#main.enter>.hero,#main.enter>details{animation:rise .42s cubic-bezier(.2,.8,.2,1) both}
#main.enter>div>:nth-child(2){animation-delay:.04s}#main.enter>div>:nth-child(3){animation-delay:.08s}#main.enter>div>:nth-child(4){animation-delay:.12s}#main.enter>div>:nth-child(n+5){animation-delay:.16s}
#main.enter>details{animation-delay:.1s}
.row-top{margin-top:12px}
/*__SOURCES_CSS__*/
@media (max-width:640px){.shell{padding:0 14px 70px}header.top{gap:10px}nav.rail{width:100%;order:3;justify-content:space-between}nav.rail button{padding:8px 5px;font-size:13px;gap:3px}.stat{min-width:90px}.card{padding:16px}}
@media (prefers-reduced-motion:reduce){*,*::before,*::after{transition:none!important;animation:none!important}}
/* Memory tree */
.memory-tree{display:grid;gap:10px}
.branch{border:1px solid var(--line);border-radius:var(--r);min-width:0;background:var(--card)}
.branch>summary{display:flex;list-style:none;min-height:54px;cursor:pointer;align-items:center;gap:12px;padding:10px 16px}
.branch>summary::-webkit-details-marker{display:none}
.branch>summary::before{content:'›';color:var(--mute);font-size:1.5rem;line-height:1;transition:rotate .15s}
.branch[open]>summary::before{rotate:90deg}
.branch-name{min-width:0;flex:1;overflow-wrap:anywhere;font-weight:650}
.branch-count{color:var(--mute);font-variant-numeric:tabular-nums}
.branch-children{list-style:none;margin:0 16px 14px 27px;padding:0 0 0 14px;border-left:1px solid var(--line);display:grid;gap:4px}
.branch-children button{width:100%;display:flex;justify-content:space-between;gap:8px;align-items:center;min-height:44px;text-align:left;background:var(--soft);border:1px solid transparent;border-radius:8px;cursor:pointer;padding:8px 12px}
.branch-children button:hover,.branch-children button:focus-visible{border-color:var(--acc)}
</style>
</head>
<body>
<div class="shell">
  <header class="top">
    <div class="brand"><span class="dot" aria-hidden="true"></span><h1>Braintwin</h1></div>
    <nav class="rail" aria-label="Sections">
      <button type="button" data-tab="start">Home</button>
      <button type="button" data-tab="memories">Memories <span class="count" id="c-inbox" hidden>0</span></button>
      <button type="button" data-tab="follow">Sources</button>
      <button type="button" data-tab="connect">Connect</button>
      <button type="button" data-tab="settings">Settings</button>
    </nav>
  </header>
  <div id="subnav"></div>
  <main id="main" tabindex="-1"></main>
  <p class="foot" id="sub"></p>
</div>
<dialog id="dlg"><div id="dlg-body"></div><div class="row row-top"><span class="grow"></span><button class="btn ghost" id="dlg-close" type="button">Close</button></div></dialog>
<div id="toast" class="toast" role="status" hidden></div>
<script nonce="__NONCE__">
(function(){
'use strict';
var CFG=window.__SB||{},HOSTED=!!CFG.api,BASE=CFG.api||'';
var QS=new URLSearchParams(location.search);
var TOKEN=HOSTED?'':(QS.get('token')||''),KEY='';
function store(k,v){try{if(v==null)sessionStorage.removeItem(k);else sessionStorage.setItem(k,v);}catch(e){}}
function stored(k){try{return sessionStorage.getItem(k)||'';}catch(e){return'';}}
if(TOKEN)store('sb.t',TOKEN);else TOKEN=stored('sb.t');
if(location.search){try{history.replaceState(null,'',location.pathname+location.hash);}catch(e){}}
var main=document.getElementById('main'),toastEl=document.getElementById('toast');
var state={unscoped:false,tagLabel:'',memoryOffset:0,tab:location.hash==='#connect'?'connect':'start',q:'',status:'approved',sort:'newest',tag:'',scope:'',from:'',platform:'',tool:'ChatGPT',overview:null,verified:false,sel:new Set()};

function h(tag,props){
  var el=document.createElement(tag);
  props=props||{};
  Object.keys(props).forEach(function(k){
    var v=props[k];
    if(k==='class')el.className=v;
    else if(k==='text')el.textContent=v;
    else if(k.slice(0,2)==='on')el.addEventListener(k.slice(2),v);
    else if(v!==false&&v!=null)el.setAttribute(k,v===true?'':v);
  });
  function add(c){if(c==null||c===false)return;if(Array.isArray(c)){c.forEach(add);return;}el.appendChild(typeof c==='string'?document.createTextNode(c):c);}
  for(var i=2;i<arguments.length;i++)add(arguments[i]);
  return el;
}
function toast(msg,err){toastEl.textContent=msg;toastEl.className='toast'+(err?' err':'');toastEl.hidden=false;clearTimeout(toast.t);toast.t=setTimeout(function(){toastEl.hidden=true},3200);}
function api(path,opts){
  opts=opts||{};
  var init={method:opts.body?'POST':'GET',headers:{}};
  if(TOKEN)init.headers['Authorization']='Bearer '+TOKEN;
  if(opts.body){init.headers['Content-Type']='application/json';
    if(HOSTED){var b={};Object.keys(opts.body).forEach(function(k){b[k]=opts.body[k]});b._dash=1;if(KEY)b._key=KEY;init.body=JSON.stringify(b);}
    else{init.headers['X-Dashboard']='1';if(KEY)init.headers['X-Dashboard-Key']=KEY;init.body=JSON.stringify(opts.body);}}
  return fetch(BASE+'/api/'+path,init).then(function(r){
    if(HOSTED&&r.status===401&&!opts.retried){return renew().then(function(ok){if(ok){if(opts.body)return{ok:false,message:'Signed in again. Submit the change when you are ready.'};opts.retried=true;return api(path,opts);}signedOut('Please sign in again.');return{ok:false,message:'Signed out'};});}
    return r.json().catch(function(){return{ok:false,message:'Bad reply'}});},function(err){
    // Apify's gateway answers an expired token with a 401 that has no CORS headers,
    // so the browser reports a network error instead. Try one refresh, then retry.
    if(opts.body)return{ok:false,message:'Could not confirm the change. Refresh and check your memory before trying again.'};
    if(HOSTED&&!opts.retried&&stored('sb.r')){return renew().then(function(ok){if(ok){opts.retried=true;return api(path,opts);}signedOut('Please sign in again.');return{ok:false,message:'Signed out'};});}
    return{ok:false,message:'Could not reach your memory. Check your connection and try again.'};}).then(function(r){
    return r;
  });
}
function q(params){return Object.keys(params).filter(function(k){return params[k]!==''&&params[k]!=null}).map(function(k){return encodeURIComponent(k)+'='+encodeURIComponent(params[k])}).join('&');}
function when(s){if(!s)return'';var d=new Date(s);return isNaN(d)?'':d.toLocaleDateString(undefined,{year:'numeric',month:'short',day:'numeric'});}

var KIND={note:'Note',preference:'Preference',fact:'Fact',decision:'Decision',agent_work:'Agent work',followed:'From a source',source_snapshot:'Web page',source_chunk:'Part of a page',digest:'Digest',profile:'About you'};
var PLATFORMS={youtube:'YouTube',facebook:'Facebook',instagram:'Instagram',linkedin:'LinkedIn',tiktok:'TikTok',x:'X',reddit:'Reddit',web:'Other websites'};
var REVIEW={tentative_statement:'It sounded tentative.',agent_work:'An agent proposed this work note.',provided_source:'This source text was supplied for review.',not_automatic:'Your saving policy calls for review.',policy_off:'You chose Ask me first.',would_replace_existing:'It could replace an earlier memory.',daily_cap_reached:'The automatic save limit was reached.',instruction_like_text:'The source contains instruction-like text.'};
function lower1(t){t=String(t||'');return t.charAt(0).toLowerCase()+t.slice(1);}
function card(m,actions){
  var tags=(m.tags||[]).filter(function(t){return !/^(follow:|scope:)/.test(t)&&['source','ingest','passage','followed','feed','dashboard'].indexOf(t)<0}).slice(0,4).map(function(t){return h('button',{class:'tag',type:'button',text:'#'+t,onclick:function(){state.tag=t;state.tagLabel='';go('memories');}})});
  var li=h('li',{class:'item'},
    h('h3',{},h('span',{text:m.title||'(untitled)'}),m.status&&m.status!=='approved'?h('span',{class:'pill '+m.status,text:m.status==='proposed'?'waiting':m.status==='decayed'?'forgotten':m.status}):null),
    h('p',{text:m.snippet||m.summary||''}),
    h('div',{class:'meta'},h('span',{class:'kind',text:m.kind||KIND[m.type]||m.type||''}),
      m.sourcePlatform?h('span',{text:PLATFORMS[m.sourcePlatform]||m.sourcePlatform}):null,
      m.sourceCoverage?h('span',{text:m.sourceCoverage}):null,
      m.appliesTo&&m.appliesTo!=='Everywhere'?h('span',{text:m.appliesTo}):null,
      h('span',{text:when(m.updatedAt||m.approvedAt)}),tags),
    m.useWhen?h('p',{class:'use',text:'AI tools use this: '+lower1(m.useWhen)}):null,
    m.origin&&m.origin.how&&m.origin.how!=='unknown'?h('p',{class:'note',text:m.origin.line}):null,
    m.status==='proposed'&&m.origin&&m.origin.reviewReasons?h('p',{class:'note',
      text:m.origin.reviewReasons.map(function(r){return REVIEW[r]||'Needs your review.';}).filter(function(t,i,a){return a.indexOf(t)===i;}).join(' ')}):null,
    h('div',{class:'row'},h('button',{class:'btn link',type:'button',text:'Open',onclick:function(){openMemory(m.id)}}),actions||null));
  return li;
}
function empty(t){return h('div',{class:'empty',text:t});}
function confirmBtn(label,fn){
  var b=h('button',{class:'btn warn',type:'button',text:label}),armed=false,timer=0;
  b.addEventListener('click',function(){
    if(!armed){armed=true;b.textContent='Sure? Click again';timer=setTimeout(function(){armed=false;b.textContent=label;},4000);return;}
    clearTimeout(timer);b.disabled=true;fn(b);});
  return b;
}
function forgetMany(ids){
  var n=0;
  (function next(){
    var id=ids.shift();
    if(!id){toast(n===1?'Forgotten':'Forgot '+n+' memories');state.sel.clear();refreshOverview();render();return;}
    api('forget',{body:{id:id}}).then(function(r){if(r.ok===false){fail(r);state.sel.clear();render();return;}n++;next();});
  })();
}

function openMemory(id){
  api('memory?id='+encodeURIComponent(id)).then(function(r){
    var m=r.memory||{},c=r.card||{};
    var body=document.getElementById('dlg-body');body.textContent='';
    body.appendChild(h('h2',{text:m.title||id}));
    body.appendChild(h('div',{class:'meta'},c.kind?h('span',{class:'kind',text:c.kind}):null,h('span',{text:m.status==='approved'?'kept':m.status==='proposed'?'waiting':m.status==='decayed'?'forgotten':m.status||''})));
    if(c.useWhen)body.appendChild(h('p',{class:'use',text:'AI tools use this: '+lower1(c.useWhen)}));
    var o=r.origin||{},u=r.usage||{};
    function fact(k,v){return v?h('p',{class:'note'},h('b',{text:k+': '}),h('span',{text:String(v)})):null;}
    [fact('Where it came from',o.line),fact('Why it was kept',o.why),fact('Your words at the time',o.sourceWords),fact('Whose words',o.trust),fact('Status',o.approval),
     fact('Replaced an older memory',o.replaces),fact('Good until',when(o.validUntil)),fact('Check first',o.check),
     fact('How AI tools use it',u.how),fact('Where it applies',u.where)].forEach(function(n){if(n)body.appendChild(n);});
    body.appendChild(h('pre',{text:m.content||m.summary||'(no content)'}));
    var cites=(m.citations||[]).map(function(c){return c&&c.url}).filter(Boolean);
    if(cites.length)body.appendChild(h('p',{class:'note',text:'Sources: '+cites.join(', ')}));
    var rel=r.related||[];
    if(rel.length){body.appendChild(h('h3',{text:'Related memories'}));body.appendChild(h('ul',{class:'list'},rel.map(function(x){return h('li',{class:'item'},h('button',{class:'btn link',type:'button',text:x.title||x.id,onclick:function(){openMemory(x.id)}}),h('span',{class:'kind',text:x.kind||''}));})));}
    if(m.status==='approved'||m.status==='proposed')body.appendChild(h('div',{class:'row row-top'},h('button',{class:'btn',type:'button',text:'Edit',onclick:function(){editMemory(m)}})));
    var dlg=document.getElementById('dlg');if(dlg.showModal)dlg.showModal();
  });
}
function editMemory(m){
  var body=document.getElementById('dlg-body');body.textContent='';
  var title=h('input',{required:true,maxlength:200,name:'title'});title.value=m.title||'';
  var content=h('textarea',{required:true,maxlength:20000,name:'content'});content.value=m.content||'';
  var tags=h('input',{name:'tags',placeholder:'comma separated'});tags.value=(m.tags||[]).join(', ');
  var f=h('form',{class:'add'},h('h2',{text:'Edit memory'}),h('label',{},'Title',title),h('label',{},'What should be remembered',content),h('label',{},'Tags',tags),
    h('div',{class:'row'},h('button',{class:'btn acc',type:'submit',text:'Save changes'}),h('span',{class:'note',text:'Costs one memory change ($0.01) if something changed.'})));
  f.addEventListener('submit',function(e){e.preventDefault();
    api('edit',{body:{id:m.id,title:title.value,content:content.value,tags:tags.value.split(',').map(function(x){return x.trim()}).filter(Boolean)}}).then(function(r){
      if(r.ok===false){if(r.error==='KEY_REQUIRED'||r.error==='BAD_KEY')return;toast(r.message||'Failed',true);return;}
      document.getElementById('dlg').close();toast(r.changed?'Saved':'Nothing changed');refreshOverview().then(render);});});
  body.appendChild(f);
}
function exportAll(btn){
  btn.disabled=true;
  var init={headers:{}};if(TOKEN)init.headers['Authorization']='Bearer '+TOKEN;
  if(HOSTED){init.method='POST';init.headers['Content-Type']='application/json';init.body=JSON.stringify({status:state.status,_dash:1,_key:KEY||undefined});}
  else if(KEY)init.headers['X-Dashboard-Key']=KEY;
  fetch(BASE+'/api/export'+(HOSTED?'':'?status='+encodeURIComponent(state.status)),init).then(function(r){if(r.status===403)throw new Error('key');if(!r.ok)throw new Error('x');return r.blob();}).then(function(b){
    var a=h('a',{href:URL.createObjectURL(b),download:'braintwin-'+state.status+'.json'});document.body.appendChild(a);a.click();
    setTimeout(function(){URL.revokeObjectURL(a.href);document.body.removeChild(a);},1000);btn.disabled=false;toast('Export downloaded');
  }).catch(function(e){btn.disabled=false;toast('Export failed',true);});
}
document.getElementById('dlg-close').addEventListener('click',function(){document.getElementById('dlg').close()});

function refreshOverview(){
  return api('overview').then(function(o){
    if(!o.ok)return;state.overview=o;
    var hl=o.health||{},by=hl.byStatus||{};
    var ci=document.getElementById('c-inbox');ci.textContent=o.inboxCount||0;ci.hidden=!((o.inboxCount||0)>0);
    document.getElementById('sub').textContent=storeLabel(hl.backend)+' · version '+(hl.version||'');if(HOSTED)signOutLink();
  });
}

function viewStart(){
  var box=h('div');main.textContent='';main.appendChild(box);
  var o=state.overview||{},by=((o.health||{}).byStatus)||{},saved=by.approved||0,waiting=o.inboxCount||0;
  var hero=h('div',{class:'hero'},h('h2',{text:saved?'What do you want to find?':'One memory for all your AI tools'}),
    h('p',{text:saved?'Search your saved memories or add something new.':'Save something worth remembering, then check that your AI tool can find it.'}));
  box.appendChild(hero);
  var si=h('input',{type:'search',placeholder:'Search your memories…','aria-label':'Search your memories'});
  var sf=h('form',{class:'search'},si,h('button',{class:'btn',type:'submit',text:'Search'}));
  sf.addEventListener('submit',function(e){e.preventDefault();state.q=si.value.trim();state.status='approved';go('memories');});
  if(saved)box.appendChild(sf);
  var qi=h('input',{placeholder:'Type something to remember, or paste a link','aria-label':'Add a memory',maxlength:2000});
  var qf=h('form',{class:'search'},qi,h('button',{class:'btn acc',type:'submit',text:'Remember'}));
  qf.addEventListener('submit',function(e){e.preventDefault();var t=qi.value.trim();if(!t)return;
    if(/^https?:\/\/\S+$/i.test(t)){toast('Reading the page…');api('link',{body:{url:t}}).then(function(r){
      if(r.ok===false){fail(r);return;}toast(r.message||'Saved',false);qi.value='';refreshOverview().then(render);});return;}
    api('add',{body:{title:t.split(/\s+/).slice(0,9).join(' ').slice(0,80),content:t,type:'auto',tags:[]}}).then(function(r){
      if(r.ok===false){fail(r);return;}toast(r.duplicate?'Already remembered':'Remembered');qi.value='';refreshOverview().then(render);});});
  box.appendChild(qf);
  box.appendChild(h('p',{class:'note',text:'A sentence is saved and sorted by itself: a rule, a fact about you, a decision or a note. A link to a page or video is read and saved, so your AI tools can use it.'}));
  if(waiting>0)box.appendChild(h('div',{class:'banner'},h('span',{text:waiting+' item'+(waiting===1?' needs':'s need')+' your eye before '+(waiting===1?'it is':'they are')+' kept.'}),h('button',{class:'btn',type:'button',text:'Review',onclick:function(){go('inbox')}})));
  var stats=h('div',{class:'stats'});
  [['Memories',saved,'memories'],['Waiting for you',waiting,'inbox'],['Following',o.sourcesFollowed||0,'follow']].forEach(function(x){
    stats.appendChild(h('button',{class:'stat',type:'button',onclick:function(){go(x[2])}},h('b',{text:String(x[1])}),h('span',{text:x[0]})))});
  box.appendChild(stats);
  if(!saved&&!state.verified)box.appendChild(h('section',{class:'card'},h('h2',{text:'Connect your first AI tool'}),
    h('p',{class:'note',text:'Choose your client, check the signed-in store, and verify that a memory saved in the client appears here.'}),
    h('div',{class:'row'},h('button',{class:'btn ghost',type:'button',text:'Connect a tool',onclick:function(){go('connect')}}))));
  var rec=(o.recentAutoApproved||[]).slice(-5).reverse();
  if(rec.length){
    var rc=h('section',{class:'card'},h('h2',{text:'Added lately'}),h('p',{class:'note',text:'Saved by itself from what you and your AI tools said.'}));var rl=h('ul',{class:'list'});
    rec.forEach(function(r){rl.appendChild(h('li',{class:'item'},h('h3',{},h('span',{text:r.title||''})),h('div',{class:'meta'},h('span',{text:when(r.at)}),/replaced:/.test(r.rule||'')?h('span',{text:(r.rule||'').replace(/^.*replaced:/,'replaced:')}):null),h('div',{class:'row'},h('button',{class:'btn link',type:'button',text:'Open',onclick:function(){openMemory(r.id)}}))))});
    rc.appendChild(rl);box.appendChild(rc);
  }
  var w=h('details',{class:'card'},h('summary',{text:'Where is my memory kept?'}),h('p',{class:'note',text:'Everything is saved in your own Apify account. Nobody else can see it, not even the maker of this tool. You can download or delete it at any time.'}));
  box.appendChild(w);
}

function viewInbox(){
  var box=h('div');main.textContent='';main.appendChild(box);
  box.appendChild(h('h2',{text:'Review when you’re ready'}));
  box.appendChild(h('p',{class:'note',text:'Tentative statements, agent work and sources that need a decision can wait here. Normal recall uses your kept memories. Keeping or declining changes costs one memory-change event.'}));
  return api('inbox').then(function(r){
    var items=r.items||[];
    if(!items.length){box.appendChild(empty('Nothing is waiting for you. Your memories are under Memories.'));return;}
    var selAll=h('button',{class:'btn ghost',type:'button',text:'Select all',onclick:function(){items.forEach(function(i){state.sel.add(i.id)});render();}});
    var appr=h('button',{class:'btn acc',type:'button',text:'Keep selected',onclick:function(){batch('approve')}});
    var rej=h('button',{class:'btn warn',type:'button',text:'Decline selected',onclick:function(){batch('reject')}});
    box.appendChild(h('div',{class:'bar'},selAll,appr,rej,h('span',{class:'note',text:state.sel.size+' selected'})));
    var ul=h('ul',{class:'list'});
    items.forEach(function(m){
      var cb=h('input',{type:'checkbox','aria-label':'Select '+(m.title||m.id),checked:state.sel.has(m.id),onchange:function(e){if(e.target.checked)state.sel.add(m.id);else state.sel.delete(m.id);render();}});
      var one=h('button',{class:'btn acc',type:'button',text:'Keep',onclick:function(){act('approve',[m.id])}});
      var no=h('button',{class:'btn warn',type:'button',text:'Decline',onclick:function(){act('reject',[m.id])}});
      ul.appendChild(card(m,h('span',{class:'row'},cb,one,no)));
    });
    box.appendChild(ul);
  });
}
function batch(kind){var ids=Array.from(state.sel);if(!ids.length){toast('Select something first',true);return;}act(kind,ids);}
function act(kind,ids){
  api(kind,{body:{ids:ids}}).then(function(r){
    if(r.ok===false)toast(r.message||'Failed',true);
    else toast(kind==='approve'?('Kept '+(r.approved!=null?r.approved:ids.length)):('Declined '+(r.rejected!=null?r.rejected:ids.length)));
    ids.forEach(function(i){state.sel.delete(i)});
    refreshOverview();render();
  });
}

function openMemoryGroup(settings){
  Object.assign(state,{q:'',status:'approved',sort:'newest',tag:'',tagLabel:'',scope:'',unscoped:false,from:'',platform:'',memoryOffset:0},settings);
  go('memories');
}
function viewLibrary(){
  var box=h('section',{class:'memory-tree','aria-label':'Memory tree'});main.textContent='';
  main.appendChild(h('div',{class:'hero'},h('h2',{text:'Memory tree'}),
    h('p',{text:'Open a project to browse your notes, agent work and saved sources.'})));
  main.appendChild(box);
  box.appendChild(empty('Loading projects…'));
  return api('library').then(function(r){
    box.textContent='';
    if(!r.ok){box.appendChild(empty(r.message||'Could not load the memory tree.'));return;}
    if(!r.projects.length){
      box.appendChild(empty('Kept memories appear here. Add a note or connect your agent to get started.'));
      box.appendChild(h('button',{class:'btn',type:'button',text:'Add a memory',onclick:function(){go('add')}}));return;
    }
    box.appendChild(h('p',{class:'note',text:r.total+' kept '+(r.total===1?'memory':'memories')}));
    r.projects.forEach(function(project,i){
      var scope=project.scope, label=scope||'Across projects';
      var parent=h('details',{class:'branch',open:state.treeOpen===scope||(state.treeOpen==null&&i===0)});
      parent.appendChild(h('summary',{},h('span',{class:'branch-name',text:label}),h('span',{class:'branch-count',text:String(project.total)})));
      var branches=h('ul',{class:'branch-children'});
      [['','All in '+label],['owner','Your notes'],['agent','Agent work'],['reference','Sources']].forEach(function(item){
        var count=item[0]?project.collections[item[0]]:project.total;
        if(!count)return;
        branches.appendChild(h('li',{},h('button',{type:'button',onclick:function(){
          state.treeOpen=scope;
          openMemoryGroup({scope:scope,unscoped:!scope,from:item[0]});
        }},h('span',{text:item[1]}),h('span',{class:'branch-count',text:String(count)}))));
      });
      parent.appendChild(branches);box.appendChild(parent);
    });
  });
}

function viewMemories(){
  var box=h('div');main.textContent='';main.appendChild(box);
  var search=h('input',{type:'search',placeholder:'Search your memories…','aria-label':'Search your memories',value:state.q});
  var sf=h('form',{class:'bar'},search,h('button',{class:'btn',type:'submit',text:'Search'}),h('button',{class:'btn acc',type:'button',text:'+ Add a memory',onclick:function(){go('add')}}));
  sf.addEventListener('submit',function(e){e.preventDefault();state.q=search.value.trim();state.sel.clear();render();});
  box.appendChild(sf);
  var from=h('div',{class:'seg',role:'group','aria-label':'Where the memory came from'});
  [['','Everything'],['owner','Your notes'],['agent','Agent work'],['reference','Sources']].forEach(function(o){
    from.appendChild(h('button',{type:'button','aria-current':String(state.from===o[0]),text:o[1],onclick:function(){state.from=o[0];state.sel.clear();render();}}))});
  box.appendChild(from);
  var project=h('input',{placeholder:'All projects',value:state.scope,'aria-label':'Project scope',maxlength:60});
  var filters=h('form',{class:'bar'},h('label',{},'Project ',project),h('button',{class:'btn ghost',type:'submit',text:'Apply'}));
  filters.addEventListener('submit',function(e){e.preventDefault();state.scope=project.value.trim();state.unscoped=false;state.sel.clear();render();});
  if(state.from==='reference'){
    var platforms=h('select',{'aria-label':'Source platform'},h('option',{value:'',text:'All sources'}));
    Object.keys(PLATFORMS).forEach(function(name){
      platforms.appendChild(h('option',{value:name,text:PLATFORMS[name],selected:state.platform===name}));
    });
    platforms.addEventListener('change',function(){state.platform=platforms.value;state.sel.clear();render();});
    filters.appendChild(h('label',{},'Source ',platforms));
  }
  box.appendChild(filters);
  if(state.unscoped)box.appendChild(h('div',{class:'bar'},h('button',{class:'tag',type:'button',text:'Across projects ×',onclick:function(){state.unscoped=false;render();}})));
  var status=h('select',{'aria-label':'Which memories to show'});
  [['approved','Kept'],['proposed','Waiting for you'],['decayed','Forgotten'],['rejected','Declined']].forEach(function(o){status.appendChild(h('option',{value:o[0],text:o[1],selected:state.status===o[0]}))});
  var sort=h('select',{'aria-label':'Order'});
  [['newest','Newest first'],['oldest','Oldest first'],['title','Title A–Z']].forEach(function(o){sort.appendChild(h('option',{value:o[0],text:o[1],selected:state.sort===o[0]}))});
  var go1=function(){state.status=status.value;state.sort=sort.value;state.sel.clear();render();};
  status.addEventListener('change',go1);sort.addEventListener('change',go1);
  var more=h('details',{open:state.status!=='approved'||state.sort!=='newest'},h('summary',{class:'note',text:'More: forgotten items, order, download'}),
    h('div',{class:'bar row-top'},h('label',{},'Show ',status),h('label',{},'Order ',sort),h('button',{class:'btn ghost',type:'button',text:'Download these',title:'Download every memory in this group as a file',onclick:function(e){exportAll(e.currentTarget)}})));
  box.appendChild(more);
  if(state.tag)box.appendChild(h('div',{class:'bar row-top'},h('button',{class:'tag',type:'button',text:(state.tagLabel||'#'+state.tag)+' ×',onclick:function(){state.tag='';state.tagLabel='';render();}})));
  var selBar=h('div',{class:'bar row-top'});box.appendChild(selBar);
  function drawSel(){selBar.textContent='';var n=state.sel.size;if(!n)return;
    selBar.appendChild(confirmBtn('Forget '+n+' selected',function(){forgetMany(Array.from(state.sel))}));
    selBar.appendChild(h('button',{class:'btn ghost',type:'button',text:'Clear selection',onclick:function(){state.sel.clear();render();}}));
    selBar.appendChild(h('span',{class:'note',text:'Forgetting costs $0.01 each. Forgotten items stay under More, in case you change your mind.'}));}
  var collection=state.from==='reference'&&state.platform?state.platform:state.from;
  var request=q({q:state.q,status:state.status,sort:state.sort,tag:state.tag,
    scope:state.scope,unscoped:state.unscoped?'1':'',collection:collection,limit:50});
  if(state.memoryRequest!==request){state.memoryRequest=request;state.memoryOffset=0;}
  return api('memories?'+request+'&offset='+state.memoryOffset).then(function(r){
    if(!r.ok){box.appendChild(empty(r.message||'Could not load memories.'));return;}
    var items=r.items||[];
    if(!items.length&&state.memoryOffset&&box.isConnected){state.memoryOffset=0;return viewMemories();}
    if(!items.length){
      box.appendChild(empty(state.q||state.from||state.tag||state.scope||state.unscoped||state.status!=='approved'?'Nothing matched. Change your filters or search words.':'No memories yet. Add one here, or just talk to a connected AI tool.'));return;}
    box.appendChild(h('p',{class:'note',text:items.length+' shown'+(state.status==='approved'?'. Tick the box on any you want to forget.':'')}));
    var ul=h('ul',{class:'list'});
    items.forEach(function(m){
      var acts=null;
      if(m.status==='approved'){
        var cb=h('input',{type:'checkbox','aria-label':'Select '+(m.title||m.id),checked:state.sel.has(m.id),onchange:function(e){if(e.target.checked)state.sel.add(m.id);else state.sel.delete(m.id);drawSel();}});
        acts=h('span',{class:'row'},confirmBtn('Forget',function(){forgetMany([m.id])}),h('label',{class:'row note'},cb,' Select'));
      }
      ul.appendChild(card(m,acts));
    });
    box.appendChild(ul);drawSel();
    if(!r.searched&&r.total>50){
      function turn(offset){state.memoryOffset=offset;state.sel.clear();render();window.scrollTo(0,0);}
      box.appendChild(h('div',{class:'bar row-top'},h('span',{class:'note',text:(r.offset+1)+'–'+(r.offset+items.length)+' of '+r.total}),
        h('button',{class:'btn ghost',type:'button',text:'Previous',disabled:!r.offset,onclick:function(){turn(r.offset-50)}}),
        h('button',{class:'btn ghost',type:'button',text:'Next',disabled:r.offset+items.length>=r.total,onclick:function(){turn(r.offset+50)}})));
    }
  });
}

function viewAdd(){
  var box=h('div');main.textContent='';main.appendChild(box);
  var title=h('input',{required:true,maxlength:200,name:'title'});
  var content=h('textarea',{required:true,maxlength:20000,name:'content'});
  var type=h('select',{name:'type'});[['auto','Let Braintwin decide'],['preference','Your rule (AI follows it every time)'],['fact','About you (true, used when relevant)'],['decision','Decision made (AI does not ask again)'],['procedure','Your way of doing a task'],['person','Person you know'],['project','Your project'],['note','Note (used when the topic comes up)']].forEach(function(t){type.appendChild(h('option',{value:t[0],text:t[1]}))});
  var tags=h('input',{placeholder:'comma separated, optional',name:'tags'});
  var ownerProject=h('input',{placeholder:'Any project',name:'scope',maxlength:60});
  var f=h('form',{class:'add'},
    h('p',{class:'note',text:'Write it the way you would tell a person. For a web page or video, paste its link on Home instead.'}),h('label',{},'Short name',title),h('label',{},'What should be remembered',content),
    h('details',{},h('summary',{class:'note',text:'More options'}),h('label',{},'Kind',type),h('label',{},'Labels',tags),h('label',{},'Project',ownerProject)),
    h('div',{class:'row'},h('button',{class:'btn acc',type:'submit',text:'Save to memory'}),h('span',{class:'note',text:'Costs one memory change ($0.01).'})));
  f.addEventListener('submit',function(e){e.preventDefault();
    api('add',{body:{title:title.value,content:content.value,type:type.value,scope:ownerProject.value,tags:tags.value.split(',').map(function(s){return s.trim()}).filter(Boolean)}}).then(function(r){
      if(r.ok===false)toast(r.message||'Failed',true);else{toast(r.duplicate?(r.message||'Already remembered'):'Saved');title.value='';content.value='';tags.value='';refreshOverview();}
    });});
  box.appendChild(f);
  var link=h('input',{type:'url',required:true,placeholder:'https://…',maxlength:2000});
  var sourceProject=h('input',{placeholder:'Any project',maxlength:60});
  var sourceText=h('textarea',{maxlength:20000,placeholder:'Optional caption, excerpt or transcript'});
  var submit=h('button',{class:'btn',type:'submit',text:'Add source'});
  var message=h('p',{class:'note',role:'status'});
  var source=h('form',{class:'add'},
    h('label',{},'Source link',link),h('label',{},'Project',sourceProject),
    h('label',{},'Text you can share',sourceText),
    h('p',{class:'note',text:'Leave text blank to read the public link. Pasted text waits for review.'}),
    h('div',{class:'row'},submit),message);
  source.addEventListener('submit',function(e){
    e.preventDefault();submit.disabled=true;
    var payload={url:link.value,scope:sourceProject.value};
    if(sourceText.value.trim())payload.text=sourceText.value;
    message.textContent=payload.text?'Filing for review…':'Reading the public source…';
    api('link',{body:payload}).then(function(r){
      message.textContent=(r.message||'Could not add the source.')+(r.coverage?' · '+r.coverage:'');
      if(r.ok&&r.saved){link.value='';sourceText.value='';refreshOverview();}
    }).finally(function(){submit.disabled=false;});
  });
  box.appendChild(h('details',{class:'card'},h('summary',{text:'Add a source'}),source));
}

function fail(r){if(r.error!=='KEY_REQUIRED'&&r.error!=='BAD_KEY')toast(r.message||'Failed',true);}
/*__SOURCES_JS__*/
function copyText(text,btn){
  function done(){var o=btn.textContent;btn.textContent='Copied';setTimeout(function(){btn.textContent=o;},1500);}
  function fallback(){var ta=h('textarea',{});ta.value=text;document.body.appendChild(ta);ta.select();var ok=false;try{ok=document.execCommand('copy');}catch(e){}document.body.removeChild(ta);if(ok)done();else toast('Select the text and copy it by hand',true);}
  if(navigator.clipboard&&navigator.clipboard.writeText)navigator.clipboard.writeText(text).then(done,fallback);else fallback();
}
function snip(out,title,note,text,label){
  var btn=h('button',{class:'btn ghost',type:'button',text:label||'Copy'});
  btn.addEventListener('click',function(){copyText(text,btn);});
  if(title)out.appendChild(h('h3',{text:title}));
  if(note)out.appendChild(h('p',{class:'note',text:note}));
  out.appendChild(h('pre',{class:'snip',text:text}));
  out.appendChild(h('div',{class:'row row-top'},btn));
}
function storeLabel(backend){
  if(backend==='sqlite')return 'Local SQLite memory';
  if(backend==='actor'||backend==='apify')return 'Memory in your Apify account';
  return 'Braintwin memory';
}
var CLIENTS=['ChatGPT','Claude','Codex','Cursor','Claude Code','VS Code','Other'];
function clientSetup(name,url){
  var auth='Authorization: Bearer YOUR_APIFY_TOKEN';
  var bridge='https://mcp.apify.com/?tools=khadinakbar/braintwin';
  if(name==='ChatGPT'||name==='Claude')return [
    'Add a custom MCP connector in '+name+' and enter this URL. Sign in with the same Apify account. The account must have access to the Braintwin Actor.',
    bridge
  ];
  if(name==='Codex')return ['Add this to ~/.codex/config.toml. Set APIFY_TOKEN in the environment that launches Codex.',
    '[mcp_servers.braintwin]\nurl = "'+url+'"\nbearer_token_env_var = "APIFY_TOKEN"'];
  if(name==='Claude Code')return ['Set APIFY_TOKEN in your shell, then run this command.', 
    'claude mcp add --transport http --header "Authorization: Bearer $APIFY_TOKEN" braintwin '+url];
  if(name==='Cursor')return ['Add the server to your personal ~/.cursor/mcp.json. Enter your Apify token in the local configuration and keep the file private.',
    JSON.stringify({mcpServers:{braintwin:{url:url,headers:{Authorization:'Bearer YOUR_APIFY_TOKEN'}}}},null,2)];
  if(name==='VS Code')return ['Merge this into your VS Code MCP configuration. VS Code prompts for your token when it starts the server.',
    JSON.stringify({inputs:[{id:'apify-token',type:'promptString',description:'Apify API token',password:true}],
      servers:{braintwin:{type:'http',url:url,headers:{Authorization:'Bearer ${input:apify-token}'}}}},null,2)];
  return ['Add a Streamable HTTP MCP server in a client that supports a Bearer token.',
    url+'\n'+auth];
}
function viewConnect(){
  var box=h('div',{class:'connect'});main.textContent='';main.appendChild(box);
  var url=(BASE||location.origin)+'/mcp';
  box.appendChild(h('div',{class:'hero'},h('h2',{text:'Connect an AI tool'}),
    h('p',{text:'Choose a client, then verify that it saves to the same memory you see here.'})));
  var pick=h('div',{class:'seg',role:'group','aria-label':'AI client'}),out=h('section',{class:'card'});
  CLIENTS.forEach(function(name){
    var button=h('button',{type:'button',text:name,'aria-pressed':String(state.tool===name),onclick:function(){
      state.tool=name;
      Array.from(pick.children).forEach(function(el){el.setAttribute('aria-pressed',String(el===button));});
      setup();
    }});
    pick.appendChild(button);
  });
  box.appendChild(pick);box.appendChild(out);
  function setup(){
    out.textContent='';
    var config=clientSetup(state.tool,url);
    out.appendChild(h('h3',{text:'Set up '+state.tool}));
    snip(out,'',config[0],config[1],'Copy setup');
  }
  setup();
  var status=h('p',{class:'note','role':'status',text:'Run a read-only check against the dashboard’s current store.'});
  var check=h('button',{class:'btn ghost',type:'button',text:'Check my store'});
  var probeStatus=h('p',{class:'note','role':'status',text:'Save the setup note in your client, then check for it here.'});
  var verify=h('button',{class:'btn',type:'button',text:'Verify the save'});
  var recall=h('div',{hidden:true});
  var probe=stored('sb.probe');
  if(!/^BT-[0-9A-F]{12}$/.test(probe)){
    var bytes=crypto.getRandomValues(new Uint8Array(6));
    probe='BT-'+Array.from(bytes,function(x){return x.toString(16).padStart(2,'0');}).join('').toUpperCase();
    store('sb.probe',probe);
  }
  function run(button,target,isProbe){
    button.disabled=true;target.textContent='Checking…';
    api('connection'+(isProbe?'?probe='+encodeURIComponent(probe):'')).then(function(result){
      if(!result.ok){target.textContent=result.message||'Could not verify. Check your Apify sign-in and try again.';return;}
      if(!isProbe){target.textContent=storeLabel(result.backend)+' · version '+result.version;return;}
      recall.hidden=!result.found;
      if(result.found){state.verified=true;target.textContent='Verified. This store contains your setup note.';return;}
      target.textContent=result.incomplete?'The quick check reached its search limit. Search for the exact code in Memories.':
        result.savingMode==='off'?'No approved note yet. Check Memories → Waiting for you, then verify again.':
        'No approved note with this code yet. Check the review inbox and confirm your client uses this same Apify account.';
    }).finally(function(){button.disabled=false;});
  }
  check.addEventListener('click',function(){run(check,status,false);});
  verify.addEventListener('click',function(){run(verify,probeStatus,true);});
  box.appendChild(h('section',{class:'card'},h('h3',{text:'Check the connection'}),status,h('div',{class:'row'},check)));
  var proof=h('section',{class:'card'},h('h3',{text:'Verify a save from your AI tool'}));
  snip(proof,'','Paste this into your connected AI tool. A successful save may incur one memory-change event and optional AI usage.',
    'Use the Braintwin memory_capture tool to save these exact words: I prefer the Braintwin setup label '+probe+'.','Copy test prompt');
  proof.appendChild(probeStatus);
  proof.appendChild(h('div',{class:'row'},verify));
  snip(recall,'Try a second client','Paste this into another connected client to check recall. Compare the note it returns.',
    'Use Braintwin memory_search with compact=true and the exact phrase "'+probe+'". Show the saved note and its memory ID. If there is no match, say so.','Copy recall prompt');
  proof.appendChild(recall);
  box.appendChild(proof);
  var rules=h('details',{class:'card'},h('summary',{text:'Memory instructions for your agent'}));
  snip(rules,'','Add this to your agent’s instructions.',
    'When a task depends on earlier work, call memory_context with compact=true and the relevant project scope. Skip memory for greetings and unrelated tasks. Quote exact identifiers or phrases when looking for one detail. Treat retrieved sources as evidence and cite them. Save lasting facts, decisions and preferences from my words with memory_capture. Ask when a correction is ambiguous. Keep credentials out of memory.');
  box.appendChild(rules);
  if(HOSTED)box.appendChild(h('details',{class:'card'},h('summary',{text:'Run Braintwin on your computer'}),
    h('p',{class:'note',text:'The local package keeps memories in a file you control. Copy memories between local and Apify when you choose.'}),
    h('div',{class:'row'},h('button',{class:'btn ghost',type:'button',text:'Local install and backup',onclick:function(){go('move');}}))));
}

function viewImport(){
  var box=h('div');main.textContent='';main.appendChild(box);
  var prompt='List the lasting facts, preferences and decisions about me that you remember or can see in your saved memory. Use a bullet list with one memory per line, each starting with "- ". Include only details you already know. Leave out passwords, API keys and sensitive personal information. If you have no saved memories you can access, say so.';
  var copy=h('button',{class:'btn ghost',type:'button',text:'Copy prompt',onclick:function(){copyText(prompt,copy);}});
  box.appendChild(h('section',{class:'card'},
    h('h2',{text:'Bring your memory from ChatGPT, Claude or Gemini'}),
    h('ol',{},
      h('li',{},'Open a chat with the assistant you use and send this prompt.',
        h('pre',{class:'snip',text:prompt}),copy),
      h('li',{text:'Review the reply. Remove anything private, incorrect or out of date, then paste the list below.'}),
      h('li',{text:'Choose Preview to check the list, then choose Import to save it to your inbox.'}))));
  box.appendChild(h('p',{class:'note',text:'Preview is free. Saving costs $0.01 in total, however many items.'}));
  var text=h('textarea',{required:true,name:'text',placeholder:'- I prefer short answers\n- My company is called ...\n\nor paste an export file here'});
  var file=h('input',{type:'file',accept:'.json,.txt,.md,application/json,text/plain'});
  file.addEventListener('change',function(){var f=file.files&&file.files[0];if(!f)return;if(f.size>900000){toast('That file is too big (900 KB max). Split it.',true);return;}var rd=new FileReader();rd.onload=function(){text.value=String(rd.result||'');};rd.readAsText(f);});
  var appr=h('input',{type:'checkbox',name:'approve'});
  var out=h('div');
  function run(preview){
    if(!text.value.trim()){toast('Paste something first',true);return;}
    api('import',{body:{text:text.value,preview:preview,approve:appr.checked}}).then(function(r){
      if(r.ok===false){fail(r);return;}
      out.textContent='';
      out.appendChild(h('h2',{text:preview?('Preview: '+(r.count||0)+' found'):('Imported '+(r.saved||0))}));
      out.appendChild(h('p',{class:'note',text:(r.duplicates||0)+' already known'+(r.skipped?(', '+r.skipped+' skipped'):'')+(r.truncated?', more than 200 found so only the first 200 were taken':'')+(preview?'. Nothing was saved yet.':(appr.checked?'. Saved as approved.':'. Waiting in your inbox.'))}));
      var ul=h('ul',{class:'list'});(r.items||[]).forEach(function(i){ul.appendChild(h('li',{class:'item'},h('h3',{},h('span',{text:i.title||'(untitled)'}),i.duplicate?h('span',{class:'pill',text:'already known'}):null)))});out.appendChild(ul);
      if(!preview){toast('Imported '+(r.saved||0));refreshOverview();}
    });
  }
  box.appendChild(h('form',{class:'add'},h('label',{},'Text or export file contents',text),h('label',{},'Or pick a file from your computer',file),
    h('label',{class:'row'},appr,h('span',{text:' Approve straight away (otherwise items wait in your inbox)'})),
    h('div',{class:'row'},h('button',{class:'btn ghost',type:'button',text:'Preview',onclick:function(){run(true)}}),h('button',{class:'btn acc',type:'button',text:'Import',onclick:function(){run(false)}}))));
  box.appendChild(out);
}

function viewSettings(){
  var box=h('div');main.textContent='';main.appendChild(box);
  var o=state.overview||{},ms=o.meaningSearch||{};
  box.appendChild(h('h2',{text:'Saving and review'}));
  box.appendChild(h('p',{class:'note',text:'Choose what happens when an AI tool or source sends you a new memory.'}));
  var sm=o.savingMode||'open',sf=h('form',{class:'add'});
  [['open','Automatic','Clear memories are saved automatically. Sensitive-looking entries wait for review.'],
   ['smart','Careful','Your own notes are kept at once. Web pages and followed items wait for you.'],
   ['off','Ask me first','Nothing is kept until you say so.']].forEach(function(m){
    var r=h('input',{type:'radio',name:'saving',value:m[0],'aria-describedby':'saving-'+m[0]});r.checked=sm===m[0];
    r.addEventListener('change',function(){
      var controls=Array.from(sf.querySelectorAll('input[name=saving]'));
      controls.forEach(function(input){input.disabled=true;});
      api('settings/saving',{body:{mode:m[0]}}).then(function(x){
        if(!x.ok){controls.forEach(function(input){input.checked=input.value===sm;});fail(x);return;}
        sm=m[0];o.savingMode=sm;toast('Saving preference updated');
      }).finally(function(){controls.forEach(function(input){input.disabled=false;});});
    });
    sf.appendChild(h('label',{class:'row'},r,h('span',{text:' '+m[1]})));
    sf.appendChild(h('p',{class:'note',id:'saving-'+m[0],text:m[2]}));
  });
  box.appendChild(sf);
  box.appendChild(h('h2',{text:'Advanced: smarter search'}));
  box.appendChild(h('p',{class:'note',text:ms.on?('On, model '+(ms.model||'')+'. Titles and summaries of your memories are sent to the service you chose, and that service bills you.'):'Off. Search already forgives typos and word forms. Turn this on to also find notes that share no words with your question, for example finding "oat milk flat white" when you ask "what do I drink in the morning". You need an account with an AI service; titles and summaries of your memories are sent to it and it bills you directly. Your key is saved in your own storage.'}));
  var url=h('input',{type:'url',placeholder:'https://…/v1',name:'url'}),model=h('input',{placeholder:'model name',name:'model'}),key=h('input',{type:'password',autocomplete:'off',placeholder:'API key',name:'key'}),dims=h('input',{type:'number',min:16,max:3072,placeholder:'optional, e.g. 256',name:'dims'});
  var mf=h('form',{class:'add'},h('label',{},'Service address',url),h('label',{},'Model name',model),h('label',{},'Your key for that service',key),h('label',{},'Size (optional, leave empty if unsure)',dims),
    h('div',{class:'row'},h('button',{class:'btn acc',type:'submit',text:ms.on?'Replace settings':'Turn on'}),
      ms.on?h('button',{class:'btn warn',type:'button',text:'Turn off',onclick:function(){api('settings/meaning',{body:{off:true}}).then(function(r){if(r.ok===false){if(r.error!=='KEY_REQUIRED'&&r.error!=='BAD_KEY')toast(r.message||'Failed',true);return;}toast('Meaning search off');refreshOverview().then(render);});}}):null));
  mf.addEventListener('submit',function(e){e.preventDefault();
    api('settings/meaning',{body:{url:url.value,model:model.value,key:key.value,dims:dims.value}}).then(function(r){if(r.ok===false){if(r.error!=='KEY_REQUIRED'&&r.error!=='BAD_KEY')toast(r.message||'Failed',true);return;}toast('Meaning search on');refreshOverview().then(render);});});
  box.appendChild(mf);
  var sc=o.smartCapture||{},ai=o.builtinAi||{};
  box.appendChild(h('h2',{text:'Smart note-taking'}));
  if(ai.available){
    var at=h('input',{type:'checkbox'});at.checked=!!ai.on;
    at.addEventListener('change',function(){api('settings/ai',{body:{on:at.checked}}).then(function(r){if(r.ok===false){fail(r);at.checked=!at.checked;return;}toast(at.checked?'Built-in AI on':'Built-in AI off');refreshOverview();});});
    box.appendChild(h('label',{class:'row'},at,h('span',{text:' Let AI pick up things I say in my own words'})));
    box.appendChild(h('p',{class:'note',text:'Clear statements like "I prefer…" are always caught for free. With this on, AI also catches the rest. It costs '+(ai.priceUsd?('$'+ai.priceUsd):'a small fee')+' only when it finds something new.'+(sc.on?' You set up your own AI key below, so the built-in one is not used.':'')}));
  }else{
    box.appendChild(h('p',{class:'note',text:'Simple rules catch clear statements such as "I prefer…" for free. To also catch things said less directly, add your own AI key below.'}));
  }
  box.appendChild(h('h2',{text:'Advanced: use your own AI key'}));
  box.appendChild(h('p',{class:'note',text:sc.on?('On, model '+(sc.model||'')+'. What you say to your agent is sent to the service you chose so it can pick out lasting facts, and that service bills you. The built-in rules still run too.'):'Off. Built-in rules already catch clear statements such as "I prefer…" or "we decided…". Turn this on to let an AI model on your own account also catch facts said in other ways. The text your agent hands over is then sent to that service. Only words you actually said are kept, and everything still passes your approval rules. Your API key is saved in your own storage.'}));
  var cu=h('input',{type:'url',placeholder:'https://…/v1',name:'url'}),cm=h('input',{placeholder:'chat model name',name:'model'}),ck=h('input',{type:'password',autocomplete:'off',placeholder:'API key',name:'key'});
  var cf=h('form',{class:'add'},h('label',{},'Service address',cu),h('label',{},'Model name',cm),h('label',{},'Your key for that service',ck),
    h('div',{class:'row'},h('button',{class:'btn acc',type:'submit',text:sc.on?'Replace settings':'Turn on'}),
      sc.on?h('button',{class:'btn warn',type:'button',text:'Turn off',onclick:function(){api('settings/capture',{body:{off:true}}).then(function(r){if(r.ok===false){if(r.error!=='KEY_REQUIRED'&&r.error!=='BAD_KEY')toast(r.message||'Failed',true);return;}toast('Smart capture off');refreshOverview().then(render);});}}):null));
  cf.addEventListener('submit',function(e){e.preventDefault();
    api('settings/capture',{body:{url:cu.value,model:cm.value,key:ck.value}}).then(function(r){if(r.ok===false){if(r.error!=='KEY_REQUIRED'&&r.error!=='BAD_KEY')toast(r.message||'Failed',true);return;}toast('Smart capture on');refreshOverview().then(render);});});
  box.appendChild(cf);
  if(ms.on&&!sc.on){
    var qm=h('input',{placeholder:'chat model name, for example the small chat model of your provider',name:'qmodel',required:true});
    var qf=h('form',{class:'add'},h('p',{class:'note',text:'Shortcut: you already turned on meaning search. Use the same service and key for smart capture; just name a chat model it offers.'}),h('label',{},'Chat model',qm),h('div',{class:'row'},h('button',{class:'btn acc',type:'submit',text:'Turn on with my existing key'})));
    qf.addEventListener('submit',function(e){e.preventDefault();api('settings/capture',{body:{sameAsMeaning:true,model:qm.value}}).then(function(r){if(r.ok===false){fail(r);return;}toast('Smart capture on');refreshOverview().then(render);});});
    box.appendChild(qf);
  }
}

var GROUP={start:'start',library:'memories',memories:'memories',inbox:'memories',add:'memories',import:'memories',move:'memories',follow:'follow',connect:'connect',settings:'settings',activity:'start'};
var ADVANCED=['Advanced: smarter search','Advanced: use your own AI key'];
function cardify(box,collapse){
  if(!box)return;
  var kids=Array.prototype.slice.call(box.children),cur=null;
  kids.forEach(function(el){
    if(el.tagName==='H2'){
      var t=el.textContent,adv=collapse||ADVANCED.indexOf(t)>=0;
      cur=adv?h('details',{class:'card'},h('summary',{text:t})):h('section',{class:'card'});
      box.insertBefore(cur,el);
      if(adv)box.removeChild(el);else cur.appendChild(el);
    }else if(cur&&!el.classList.contains('card')){cur.appendChild(el);}
  });
}
function enter(){main.classList.remove('enter');void main.offsetWidth;main.classList.add('enter');clearTimeout(enter.t);enter.t=setTimeout(function(){main.classList.remove('enter')},900);}
function go(t){if(t==='activity')t='start';state.tab=t;state.sel.clear();enter();render();try{window.scrollTo(0,0);}catch(e){}}
function render(){
  var g=GROUP[state.tab]||'start';
  document.querySelectorAll('nav.rail button').forEach(function(b){b.setAttribute('aria-current',String(b.dataset.tab===g))});
  var sn=document.getElementById('subnav');sn.textContent='';
  if(g==='memories'){
    var w=(state.overview||{}).inboxCount||0,seg=h('div',{class:'seg'});
    [['memories','All memories'],['library','Memory tree'],['inbox','Waiting for you'+(w?' ('+w+')':'')],['add','Add a memory'],['import','Bring in old notes'],['move','Move or back up']].forEach(function(x){
      seg.appendChild(h('button',{type:'button','aria-current':String(state.tab===x[0]),text:x[1],onclick:function(){go(x[0])}}))});
    sn.appendChild(seg);
  }
  var v={start:viewStart,inbox:viewInbox,memories:viewMemories,library:viewLibrary,add:viewAdd,settings:viewSettings,follow:viewFollow,connect:viewConnect,import:viewImport,move:viewMove}[state.tab]||viewStart;
  var tab=state.tab;
  Promise.resolve(v()).then(function(){if(tab==='settings')cardify(main.firstChild);}).catch(function(){main.textContent='';main.appendChild(empty('Could not load. Open the page again from your Apify link.'));});
  if(tab==='settings')cardify(main.firstChild);
}
document.querySelectorAll('nav.rail button').forEach(function(b){b.addEventListener('click',function(){if(HOSTED&&!TOKEN)return;go(b.dataset.tab)})});

/* ---- hosted page: sign in with Apify (no password or key is typed here; nothing is kept on our side) ---- */
var OA=CFG.oauth||{};
function b64u(buf){var s='',a=new Uint8Array(buf);for(var i=0;i<a.length;i++)s+=String.fromCharCode(a[i]);return btoa(s).replace(/\+/g,'-').replace(/\//g,'_').replace(/=+$/,'');}
function rnd(n){var a=new Uint8Array(n);crypto.getRandomValues(a);return b64u(a);}
function redirectUri(){return OA.redirectUri||(location.origin+location.pathname);}
function signIn(){
  var verifier=rnd(48),st=rnd(16);store('sb.v',verifier);store('sb.s',st);
  store('sb.view',state.tab==='connect'?'connect':null);
  crypto.subtle.digest('SHA-256',new TextEncoder().encode(verifier)).then(function(d){
    location.assign(OA.authorize+'?'+q({response_type:'code',client_id:OA.clientId,redirect_uri:redirectUri(),scope:OA.scope||'full_api_access',state:st,code_challenge:b64u(d),code_challenge_method:'S256'}));});
}
function tokenCall(params){
  params.client_id=OA.clientId;
  return fetch(OA.token,{method:'POST',headers:{'Content-Type':'application/x-www-form-urlencoded'},body:q(params)}).then(function(r){return r.json().catch(function(){return{}})}).then(function(j){
    if(!j||!j.access_token)return false;TOKEN=j.access_token;store('sb.t',TOKEN);if(j.refresh_token)store('sb.r',j.refresh_token);return true;}).catch(function(){return false;});
}
function renew(){var r=stored('sb.r');if(!r)return Promise.resolve(false);return tokenCall({grant_type:'refresh_token',refresh_token:r});}
function signedOut(msg){
  TOKEN='';KEY='';state.overview=null;state.verified=false;['sb.t','sb.r','sb.k','sb.v','sb.s','sb.probe','sb.view'].forEach(function(k){store(k,null)});
  document.getElementById('subnav').textContent='';main.textContent='';document.body.classList.add('out');
  document.querySelectorAll('nav.rail button').forEach(function(b){b.setAttribute('aria-current','false')});
  var btn=h('button',{class:'btn acc',type:'button',text:'Sign in with Apify',onclick:signIn});
  enter();
  main.appendChild(h('div',{class:'hero'},h('span',{class:'eyebrow',text:'Your dashboard'}),h('h2',{text:'One memory for all your AI tools'}),
    h('p',{text:msg||'Sign in to see and manage what your AI tools remember. Your memories stay in your own Apify account; this page keeps nothing.'}),
    h('div',{class:'row'},btn),
    h('ul',{class:'trust'},h('li',{text:'Stays in your Apify account'}),h('li',{text:'No extra password'}),h('li',{text:'Sign out any time'}))));
  main.appendChild(h('details',{class:'card'},h('summary',{text:'What does signing in allow?'}),h('p',{class:'note',text:'Apify asks you to allow this page to use your account. The permission stays in this browser tab and ends when you close it or sign out. Nothing is sent to anyone but Apify.'})));
  var sub=document.getElementById('sub');sub.textContent='';sub.appendChild(h('a',{class:'btn link',href:'../',text:'What is Braintwin?'}));
}
function signOutLink(){
  var f=document.getElementById('sub');if(!f||document.getElementById('so'))return;
  f.appendChild(document.createTextNode(' · '));f.appendChild(h('button',{class:'btn link',id:'so',type:'button',text:'Sign out',onclick:function(){signedOut('You are signed out.')}}));
}
function boot(){document.body.classList.remove('out');enter();refreshOverview().then(function(){if(HOSTED&&!TOKEN)return;render();if(HOSTED)signOutLink();});}
if(!HOSTED){boot();}
else if(QS.get('code')){
  var okState=QS.get('state')&&QS.get('state')===stored('sb.s'),ver=stored('sb.v');store('sb.v',null);store('sb.s',null);
  if(stored('sb.view')==='connect')state.tab='connect';
  store('sb.view',null);
  if(!okState||!ver){signedOut('Sign-in could not be finished. Please try again.');}
  else{main.appendChild(empty('Signing you in…'));tokenCall({grant_type:'authorization_code',code:QS.get('code'),redirect_uri:redirectUri(),code_verifier:ver}).then(function(ok){if(ok)boot();else signedOut('Sign-in could not be finished. Please try again.');});}
}
else if(TOKEN){boot();}
else{signedOut(QS.get('error')?'Sign-in was cancelled.':'');}
})();
</script>
</body>
</html>
"""


_PAGE = _BASE.replace('/*__SOURCES_CSS__*/', SOURCES_CSS.strip()).replace('/*__SOURCES_JS__*/', SOURCES_JS.strip())


def render_page(nonce: str) -> str:
    return _PAGE.replace('__NONCE__', nonce)
