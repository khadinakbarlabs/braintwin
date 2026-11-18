"""The Sources and Move screens of the dashboard (styles and script).

These two strings are placed into the single style block and the single script block of the page in
`dashboard_page.py`, so they share its helpers (`h`, `api`, `toast`, `card`, `confirmBtn`, `snip`).
Like the rest of the page, every piece of stored text is written with `textContent` only.
"""

from __future__ import annotations

SOURCES_CSS = r"""
ol.flow{list-style:none;margin:0 0 18px;padding:0;display:grid;grid-template-columns:repeat(4,1fr);gap:0;counter-reset:f;border:1px solid var(--line);border-radius:var(--r);background:var(--card);overflow:hidden}
ol.flow li{counter-increment:f;padding:14px 16px 15px;position:relative;font-size:14px;color:var(--mute);border-left:1px solid var(--line)}
ol.flow li:first-child{border-left:0}
ol.flow li::before{content:'0' counter(f);display:block;font:400 1.35rem/1 var(--serif);color:var(--acc);margin-bottom:6px;letter-spacing:-.02em}
ol.flow li b{display:block;color:var(--ink);font-weight:650;font-size:15px}
.kinds{display:grid;grid-template-columns:repeat(4,1fr);gap:8px;margin-bottom:14px}
.kinds button{background:var(--card);border:1px solid var(--line);border-radius:12px;padding:11px 12px;text-align:left;cursor:pointer;display:grid;gap:2px;transition:border-color .15s,box-shadow .15s}
.kinds button b{font-weight:650;font-size:14.5px}
.kinds button span{color:var(--mute);font-size:12.5px;line-height:1.35}
.kinds button:hover{border-color:var(--acc)}
.kinds button[aria-pressed=true]{border-color:var(--acc);background:var(--tint);box-shadow:0 0 0 3px var(--tint)}
.kinds button:focus-visible{outline:2px solid var(--acc);outline-offset:2px}
fieldset.choice{border:0;margin:0 0 4px;padding:0;display:grid;gap:8px}
fieldset.choice legend{font-weight:650;padding:0;margin-bottom:8px}
li.src{gap:9px;padding:16px 18px}
li.src.open{border-color:var(--acc)}
.tally{display:flex;gap:8px;flex-wrap:wrap}
.tally span{background:var(--soft);border-radius:10px;padding:6px 11px;font-size:13px;color:var(--mute)}
.tally span b{color:var(--ink);font:400 1.05rem/1 var(--serif);margin-right:5px}
.tally span.hot{background:var(--warn-tint);color:var(--warn)}
.tally span.hot b{color:var(--warn)}
.problem{background:var(--warn-tint);color:var(--warn);border-radius:10px;padding:8px 11px;font-size:13.5px;margin:0}
.panel{border-top:1px solid var(--line);margin-top:6px;padding-top:14px;display:grid;gap:10px}
.panel .seg{margin-bottom:0}
.panel ul.list li.item{background:var(--soft);border-color:transparent}
.leave{background:var(--soft);border-radius:12px;padding:14px;display:grid;gap:10px}
.leave p{margin:0;font-size:14px;color:var(--mute)}
.duo{display:grid;grid-template-columns:1fr 1fr;gap:12px;margin-bottom:16px}
.duo .card{margin:0}
.way{font-size:12px;font-weight:700;letter-spacing:.08em;text-transform:uppercase;color:var(--acc)}
@media (max-width:640px){ol.flow{grid-template-columns:1fr 1fr}ol.flow li:nth-child(3){border-left:0}ol.flow li:nth-child(n+3){border-top:1px solid var(--line)}.kinds{grid-template-columns:1fr 1fr}.duo{grid-template-columns:1fr}}
"""

SOURCES_JS = r"""
var SRC_KINDS=[
  ['youtube','YouTube channel','Every new video','Paste the channel link, for example youtube.com/@channel','Link to the channel'],
  ['blog','Blog','Every new post','Paste the blog address, for example example.com/blog','Link to the blog'],
  ['feed','Newsletter or feed','Every new issue','Paste the newsletter or feed link','Link to the newsletter or its feed'],
  ['once','One page or video','Saved once, not followed','','']];
var SRC_PAGE=50;
function srcLabel(fw){return fw.kind==='youtube'?'YouTube':'Blog or newsletter';}
function plural(n,w){return n+' '+w+(n===1?'':'s');}
function viewFollow(){
  var box=h('div');main.style.minHeight=main.offsetHeight+'px';main.textContent='';main.appendChild(box);
  function settle(){main.style.minHeight='';}
  if(!state.srcKind)state.srcKind='youtube';
  box.appendChild(h('div',{class:'hero'},h('h2',{text:'Sources that keep your memory fresh'}),
    h('p',{text:'Follow the channels, blogs and newsletters you learn from. Braintwin reads what is new and saves a short note, so your AI tools know what you read and watch.'})));
  box.appendChild(h('ol',{class:'flow'},
    h('li',{},h('b',{text:'You add a source'}),'A channel, blog or newsletter'),
    h('li',{},h('b',{text:'It gets checked'}),'Once a day, or when you ask'),
    h('li',{},h('b',{text:'You decide'}),'Keep or decline each new item'),
    h('li',{},h('b',{text:'Your AI knows it'}),'In every connected tool')));
  var addBox=h('section',{class:'card'});box.appendChild(addBox);
  var listBox=h('section',{class:'card'});box.appendChild(listBox);
  var autoBox=h('section',{class:'card'});box.appendChild(autoBox);
  var dBox=h('section',{class:'card'});box.appendChild(dBox);

  function drawAdd(){
    addBox.textContent='';addBox.appendChild(h('h2',{text:'Add a source'}));
    var kinds=h('div',{class:'kinds',role:'group','aria-label':'Kind of source'});
    SRC_KINDS.forEach(function(k){
      kinds.appendChild(h('button',{type:'button','aria-pressed':String(state.srcKind===k[0]),onclick:function(){state.srcKind=k[0];drawAdd();}},h('b',{text:k[1]}),h('span',{text:k[2]})));});
    addBox.appendChild(kinds);
    var kind=SRC_KINDS.filter(function(k){return k[0]===state.srcKind})[0]||SRC_KINDS[0];
    if(kind[0]==='once'){
      addBox.appendChild(h('p',{class:'note',text:'A single page or video is not a source, because nothing new will come from it. Paste its link on Home and it is saved once.'}));
      addBox.appendChild(h('div',{class:'row'},h('button',{class:'btn acc',type:'button',text:'Go to Home',onclick:function(){go('start')}})));
      return;
    }
    var target=h('input',{required:true,maxlength:500,placeholder:kind[3],name:'target'});
    var ask=h('input',{type:'radio',name:'mode',value:'ask'}),keep=h('input',{type:'radio',name:'mode',value:'keep'});ask.checked=true;
    var busy=false;
    var f=h('form',{class:'add'},h('label',{},kind[4],target),
      h('fieldset',{class:'choice'},h('legend',{text:'What should happen with new items?'}),
        h('label',{class:'row'},ask,h('span',{text:' Ask me first. New items wait under the source until I keep or decline them.'})),
        h('label',{class:'row'},keep,h('span',{text:' Keep them without asking. Best for sources I fully trust.'}))),
      h('div',{class:'row'},h('button',{class:'btn acc',type:'submit',text:'Add source'}),h('span',{class:'note',text:'Adding and checking are free. Each new item saved costs $0.01.'})));
    f.addEventListener('submit',function(e){e.preventDefault();if(busy)return;busy=true;toast('Looking for the feed…');
      api('follows/add',{body:{target:target.value,autoApprove:keep.checked}}).then(function(r){busy=false;
        if(r.ok===false){fail(r);return;}toast(r.duplicate?'You already have this source':'Source added');refreshOverview().then(render);});});
    addBox.appendChild(f);
  }

  function drawAuto(a){autoBox.textContent='';autoBox.appendChild(h('h2',{text:'Checking for new items'}));var t=h('input',{type:'checkbox'});t.checked=!!(a&&a.followDaily);
    t.addEventListener('change',function(){api('follows/auto',{body:{on:t.checked}}).then(function(r){if(r.ok===false){fail(r);t.checked=!t.checked;return;}toast(t.checked?'Daily check on':'Daily check off');});});
    autoBox.appendChild(h('label',{class:'row'},t,h('span',{text:' Check all my sources by itself, once a day'})));
    autoBox.appendChild(h('p',{class:'note',text:'Saving something new costs $0.01.'+(a&&a.lastFollowCheck?' Last automatic check: '+String(a.lastFollowCheck).slice(0,16).replace('T',' ')+' UTC.':'')}));}

  function drawDigest(d){dBox.textContent='';d=d||{};
    dBox.appendChild(h('h2',{text:'Summary of what you learned'}));
    var cost=d.usesOwnKey?'Because you use your own AI key, a digest costs one memory change ($0.01).':(d.priceUsd?('Each digest costs $'+d.priceUsd+'.'):'');
    dBox.appendChild(h('p',{class:'note',text:d.possible?('Let an AI sum up what your followed videos, blogs and saved links taught you, written for you. '+cost+' Nothing new, no charge.'):'Turn on AI note-taking in Settings to use this.'}));
    if(!d.possible)return;
    var sel=h('select',{name:'every'});[['off','Off'],['daily','Every day'],['weekly','Every week'],['monthly','Every month']].forEach(function(o){var op=h('option',{value:o[0],text:o[1]});if((d.every||'off')===o[0])op.selected=true;sel.appendChild(op);});
    sel.addEventListener('change',function(){api('settings/digest',{body:{every:sel.value}}).then(function(r){if(r.ok===false){fail(r);return;}toast(sel.value==='off'?'Digest off':'Digest: '+sel.options[sel.selectedIndex].text.toLowerCase());drawDigest(r.digest);});});
    var nb=h('button',{class:'btn acc',type:'button',text:'Write one now'});
    nb.addEventListener('click',function(){nb.disabled=true;toast('Writing your digest…');api('digest/now',{body:{}}).then(function(r){nb.disabled=false;toast(r.message||(r.ok===false?'Failed':'Done'),r.ok===false);if(r.digest)drawDigest(r.digest);if(r.made)refreshOverview();});});
    dBox.appendChild(h('div',{class:'row'},h('label',{},'How often ',sel),nb));
    if(d.lastAt)dBox.appendChild(h('p',{class:'note',text:'Last time: '+String(d.lastAt).slice(0,16).replace('T',' ')+' UTC. '+(d.lastNote||'')+(d.lastId?' Find it under Memories by searching "digest".':'')}));
  }

  function check(id,btn){
    btn.disabled=true;toast('Checking…');
    api('follows/sync',{body:id?{id:id}:{}}).then(function(r){btn.disabled=false;
      if(r.ok===false&&!r.feeds){fail(r);return;}
      var bad=(r.feeds||[]).filter(function(x){return!x.ok}).length;
      toast(plural(r.saved||0,'new item')+(bad?(', '+plural(bad,'source')+' could not be read'):''),bad>0&&!(r.saved>0));
      refreshOverview().then(render);});
  }
  function decide(kind,ids){
    if(!ids.length)return;
    api(kind,{body:kind==='reject'?{ids:ids,reason:'declined from its source'}:{ids:ids}}).then(function(r){
      if(r.ok===false){fail(r);return;}
      toast(kind==='approve'?('Kept '+(r.approved!=null?r.approved:ids.length)):('Declined '+(r.rejected!=null?r.rejected:ids.length)));
      refreshOverview().then(render);});
  }

  function drawPanel(li,fw){
    var tab=state.srcTab==='kept'?'kept':'waiting';
    var panel=h('div',{class:'panel'});li.appendChild(panel);
    var seg=h('div',{class:'seg',role:'group','aria-label':'Items from this source'});
    [['waiting','Waiting for you ('+(fw.waiting||0)+')'],['kept','Kept ('+(fw.saved||0)+')']].forEach(function(x){
      seg.appendChild(h('button',{type:'button','aria-current':String(tab===x[0]),text:x[1],onclick:function(){state.srcTab=x[0];render();}}));});
    panel.appendChild(seg);
    var body=h('div',{},empty('Loading…'));panel.appendChild(body);
    api('memories?'+q({tag:'follow:'+fw.id,status:tab==='kept'?'approved':'proposed',limit:SRC_PAGE})).then(function(r){
      body.textContent='';setTimeout(settle,0);
      if(r.ok===false){body.appendChild(empty(r.message||'Could not load these items.'));return;}
      var items=r.items||[],ids=items.map(function(m){return m.id});
      if(!items.length){body.appendChild(empty(tab==='kept'?'Nothing kept from this source yet.':(fw.autoApprove?'Nothing is waiting. This source keeps new items without asking.':'Nothing is waiting. Press Check now to look for new items.')));return;}
      if(tab==='waiting'){
        body.appendChild(h('p',{class:'note',text:'Keep what is useful, decline the rest. Declined items are never shown to your AI tools. One action costs $0.01, however many items.'}));
        body.appendChild(h('div',{class:'bar'},
          h('button',{class:'btn acc',type:'button',text:'Keep all '+items.length,onclick:function(){decide('approve',ids)}}),
          confirmBtn('Decline all '+items.length,function(){decide('reject',ids)})));
      }else{
        body.appendChild(h('p',{class:'note',text:'These are in your memory now. Forget one to take it out ($0.01).'}));
      }
      var ul=h('ul',{class:'list'});
      items.forEach(function(m){
        var actions=tab==='waiting'
          ?[h('button',{class:'btn',type:'button',text:'Keep',onclick:function(){decide('approve',[m.id])}}),h('button',{class:'btn warn',type:'button',text:'Decline',onclick:function(){decide('reject',[m.id])}})]
          :[confirmBtn('Forget',function(){api('forget',{body:{id:m.id,reason:'removed from its source'}}).then(function(x){if(x.ok===false){fail(x);render();return;}toast('Forgotten');refreshOverview().then(render);});})];
        ul.appendChild(card(m,actions));});
      body.appendChild(ul);
      if((r.total||0)>items.length)body.appendChild(h('p',{class:'note',text:'Showing the newest '+items.length+' of '+r.total+'.'}));
    });
  }

  function drawLeave(li,fw){
    var all=(fw.saved||0)+(fw.waiting||0);
    function gone(forget){return function(){api('follows/remove',{body:{id:fw.id,forget:forget}}).then(function(x){
      state.srcLeave='';
      if(x.ok===false){fail(x);render();return;}
      toast(forget?('Source removed and '+plural(x.forgotten||0,'item')+' cleared'):'Source removed. What it saved stays in Memories.');
      if(state.src===fw.id)state.src='';refreshOverview().then(render);});};}
    li.appendChild(h('div',{class:'leave'},
      h('h3',{text:'Stop following this source?'}),
      h('p',{text:'It will no longer be checked. Choose what happens to the '+plural(all,'item')+' it brought in.'}),
      h('div',{class:'row'},
        h('button',{class:'btn',type:'button',text:'Remove, keep its memories',onclick:gone(false)}),
        all?confirmBtn('Remove and clear all '+all,gone(true)):null,
        h('button',{class:'btn link',type:'button',text:'Cancel',onclick:function(){state.srcLeave='';render();}})),
      all?h('p',{text:'Clearing forgets the kept items and declines the waiting ones. It costs $0.01 in total.'}):null));
  }

  drawAdd();
  return api('follows').then(function(r){
    drawAuto(r&&r.auto);drawDigest(r&&r.digest);
    var items=(r&&r.follows)||[],kept=0,waiting=0;
    items.forEach(function(fw){kept+=fw.saved||0;waiting+=fw.waiting||0;});
    listBox.appendChild(h('h2',{text:'Your sources ('+items.length+')'}));
    if(!items.length){listBox.appendChild(empty('No sources yet. Add your first one above.'));settle();return;}
    var all=h('button',{class:'btn',type:'button',text:'Check all now',onclick:function(){check(null,all)}});
    listBox.appendChild(h('div',{class:'bar'},all,h('span',{class:'grow'}),
      h('div',{class:'tally'},h('span',{},h('b',{text:String(kept)}),'kept'),h('span',{class:waiting?'hot':''},h('b',{text:String(waiting)}),'waiting for you'))));
    var ul=h('ul',{class:'list'});
    items.forEach(function(fw){
      var open=state.src===fw.id,w=fw.waiting||0;
      var one=h('button',{class:'btn ghost',type:'button',text:'Check now'});one.addEventListener('click',function(){check(fw.id,one)});
      var sw=h('input',{type:'checkbox'});sw.checked=!!fw.autoApprove;
      sw.addEventListener('change',function(){api('follows/update',{body:{id:fw.id,autoApprove:sw.checked}}).then(function(x){
        if(x.ok===false){fail(x);sw.checked=!sw.checked;return;}toast(sw.checked?'New items are kept without asking':'New items will wait for you');render();});});
      var li=h('li',{class:'item src'+(open?' open':'')},
        h('h3',{},h('span',{text:fw.title||fw.feedUrl||''}),h('span',{class:'pill',text:srcLabel(fw)}),
          h('span',{class:'pill '+(fw.autoApprove?'approved':''),text:fw.autoApprove?'kept without asking':'asks you first'})),
        h('div',{class:'tally'},h('span',{},h('b',{text:String(fw.saved||0)}),'kept'),h('span',{class:w?'hot':''},h('b',{text:String(w)}),'waiting'),
          h('span',{text:fw.lastSyncAt?('checked '+when(fw.lastSyncAt)):'never checked'})),
        fw.lastError?h('p',{class:'problem',text:'The last check could not read this source: '+fw.lastError}):null,
        h('div',{class:'row'},
          h('button',{class:'btn'+(w&&!open?' acc':''),type:'button','aria-expanded':String(open),text:open?'Close':(w?'Review '+w+' waiting':'See what it saved'),
            onclick:function(){state.src=open?'':fw.id;state.srcTab=w?'waiting':'kept';state.srcLeave='';render();}}),
          one,h('span',{class:'grow'}),
          h('button',{class:'btn warn',type:'button',text:'Remove',onclick:function(){state.srcLeave=state.srcLeave===fw.id?'':fw.id;render();}})),
        h('label',{class:'row'},sw,h('span',{text:' Keep new items from this source without asking me'})));
      if(state.srcLeave===fw.id)drawLeave(li,fw);
      if(open)drawPanel(li,fw);
      ul.appendChild(li);
    });
    listBox.appendChild(ul);
    if(!items.some(function(fw){return fw.id===state.src}))settle();
  });
}

function viewMove(){
  var box=h('div');main.textContent='';main.appendChild(box);
  box.appendChild(h('div',{class:'hero'},h('h2',{text:'One memory, in the cloud or on your computer'}),
    h('p',{text:'Use Braintwin in the cloud, on your own computer, or both. You can copy your memory in either direction at any time, and running a copy twice never makes duplicates.'})));
  var up=h('section',{class:'card'},h('span',{class:'way',text:'Your computer → cloud'}),h('h2',{text:'Send local memory to the cloud'}));
  snip(up,'','For when you started on your own computer and now want the same memory in ChatGPT, Claude on the web and other online tools.','~/.braintwin/venv/bin/braintwin-cli move --from sqlite --to apify');
  var down=h('section',{class:'card'},h('span',{class:'way',text:'Cloud → your computer'}),h('h2',{text:'Bring cloud memory to your computer'}));
  snip(down,'','For when you want a private copy that works on your own computer, even without internet.','~/.braintwin/venv/bin/braintwin-cli move --from apify --to sqlite');
  box.appendChild(h('div',{class:'duo'},up,down));
  var how=h('details',{class:'card'},h('summary',{text:'How to run these two commands'}));box.appendChild(how);
  how.appendChild(h('ul',{class:'list steps'},
    h('li',{class:'item'},h('h3',{text:'Install Braintwin on your computer first'}),h('p',{text:'One line, shown under Run Braintwin on your own computer below. Then open a terminal.'})),
    h('li',{class:'item'},h('h3',{text:'Set your Apify token once'}),h('p',{text:'It is under Settings, API and Integrations in Apify Console. Keep it private.'})),
    h('li',{class:'item'},h('h3',{text:'Try it first'}),h('p',{text:'Add --dry-run to the command to see what would be copied. Nothing is written.'})),
    h('li',{class:'item'},h('h3',{text:'Run the command'}),h('p',{text:'Only kept memories are copied. Waiting, declined and forgotten items stay where they are.'}))));
  snip(how,'','Set the token (Mac or Linux), and add the small helper the copy needs, once:','export APIFY_TOKEN=YOUR_APIFY_TOKEN\n~/.braintwin/venv/bin/python -m pip install apify-client');
  var file=h('section',{class:'card'},h('h2',{text:'Or move it as a file'}));box.appendChild(file);
  file.appendChild(h('p',{class:'note',text:'No terminal needed. Download everything you have kept as one file, for a backup or to carry it somewhere else. To bring a file in, use Bring in old notes.'}));
  var dl=h('button',{class:'btn acc',type:'button',text:'Download my memory'});
  dl.addEventListener('click',function(){var keep=state.status;state.status='approved';exportAll(dl);state.status=keep;});
  file.appendChild(h('div',{class:'row'},dl,h('button',{class:'btn ghost',type:'button',text:'Bring in a file',onclick:function(){go('import')}})));
  if(!HOSTED)return;
  var local=h('section',{class:'card'},h('h2',{text:'Run Braintwin on your own computer'}));box.appendChild(local);
  local.appendChild(h('p',{class:'note',text:'Free. Your memories are kept in one private file on your disk and nothing is sent anywhere. Needs Python 3.11 or newer.'}));
  snip(local,'1. Install','Paste this one line into your terminal. It also prints the exact line for each AI tool.','curl -fsSL https://raw.githubusercontent.com/khadinakbarlabs/braintwin/main/install.sh | bash');
  snip(local,'2. Connect Claude Code','Run once, then restart Claude Code.','claude mcp add braintwin -- ~/.braintwin/venv/bin/braintwin');
  snip(local,'Cursor, Claude Desktop and others','Add a local (command) MCP server. Use the full path the installer printed.',JSON.stringify({mcpServers:{braintwin:{command:'/FULL/PATH/TO/.braintwin/venv/bin/braintwin'}}},null,2));
}
"""
