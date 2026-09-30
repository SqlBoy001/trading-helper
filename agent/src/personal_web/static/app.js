'use strict';
const $ = id => document.getElementById(id);
let draft = null;
let imageId = null, correction = null, nextAfter = null, request = null, busy = false;
const names = {execution:'成交',snapshot:'账户快照',intent:'事前想法',plan:'计划',correction:'更正',review:'复盘',reminder:'提醒',decision:'决定',account_activity:'账户业务'};
function status(text) { $('status').textContent = text; }
async function api(path, body, binary=false) {
  const response = await fetch(path, body === undefined ? {} : {method:'POST',headers:{'Content-Type':binary?'application/octet-stream':'application/json'},body:binary?body:JSON.stringify(body)});
  const result = await response.json();
  if (!response.ok) { if(response.status===401) lock(); throw new Error(result.error || '请求失败'); }
  return result;
}
function lock() { $('login').hidden=false; $('workspace').hidden=true; $('logout').hidden=true; }
function unlock() { $('login').hidden=true; $('workspace').hidden=false; $('logout').hidden=false; $('key').value=''; loadOverview().catch(e=>status(e.message)); }
async function loadOverview(){
 const result=await api('/api/overview'), box=$('overview-content');box.replaceChildren();
 const counts=document.createElement('p');counts.textContent='已记录 '+result.execution_count+' 条买卖流水、'+result.activity_count+' 条其他业务。';box.append(counts);
 if(result.initialization){const note=document.createElement('p');note.textContent=result.initialization.coverage_note;box.append(note);}
 if(result.snapshot){const d=result.snapshot.data;const p=document.createElement('p');p.textContent=(d.as_of_date||new Date(result.snapshot.occurred_at).toLocaleDateString())+' · '+(d.market_session==='after_close'?'收盘后':'账户快照')+' · '+d.positions.length+' 项持仓'+(d.market_value!=null?' · 市值 '+d.market_value.toLocaleString('zh-CN',{minimumFractionDigits:2})+' 元':'')+' · 可用资金 '+(d.cash==null?'未知':d.cash.toLocaleString('zh-CN',{minimumFractionDigits:2})+' 元');box.append(p);
 const details=document.createElement('details'), title=document.createElement('summary');title.textContent='展开最新持仓';details.append(title);for(const pos of d.positions){const row=document.createElement('p');row.textContent=(pos.name||pos.symbol)+' ('+pos.symbol+')：'+pos.quantity+' 股/份'+(pos.market_value!=null?'，市值 '+pos.market_value.toLocaleString('zh-CN')+' 元':'');details.append(row);}box.append(details);
 }else{const p=document.createElement('p');p.textContent='尚无持仓快照；成交不会自动当作当前持仓。';box.append(p);}
}
function localTime(value=new Date()) { const d=new Date(value); return new Date(d.getTime()-d.getTimezoneOffset()*60000).toISOString().slice(0,16); }
function tab(name) { for(const n of ['capture','timeline','review']) $(n).hidden=n!==name; if(name==='timeline') loadHistory().catch(e=>status(e.message)); }
function fields() { const kind=$('kind').value; for(const [id,k] of [['trade-fields','execution'],['snapshot-fields','snapshot'],['intent-fields','intent'],['plan-fields','plan']]) $(id).hidden=kind!==k; $('time-label').hidden=kind==='plan'||!!correction; $('when').required=kind!=='plan'&&!correction; }
function clearImage(){ imageId=null;draft=null;$('apply-draft').hidden=true;$('file').value='';$('image-panel').hidden=true;$('preview').removeAttribute('src');$('ocr').value='';request=null; }
$('connect').onclick=async()=>{try{await api('/api/login',{key:$('key').value});unlock();status('已连接本地账本');}catch(e){status(e.message);}};
$('logout').onclick=async()=>{try{await api('/api/logout',{});location.reload();}catch(e){status(e.message);}};
document.querySelectorAll('[data-tab]').forEach(b=>b.onclick=()=>tab(b.dataset.tab));
$('kind').onchange=fields;
$('when').value=localTime();
$('clear-image').onclick=clearImage;
$('apply-draft').onclick=()=>{if(!draft)return;$('kind').value='execution';fields();$('symbol').value=draft.symbol;$('side').value=draft.side;$('quantity').value=draft.quantity;$('price').value=draft.price;if(draft.when)$('when').value=draft.when;$('confirmed').checked=false;status('已填入识别候选。请逐项对照原图，尤其是数量、价格和成交时间。');};
$('file').onchange=async()=>{
 const file=$('file').files[0]; if(!file)return;
 if(file.size>12*1024*1024){status('截图超过 12 MB');return;}
 busy=true;$('save').disabled=true;imageId=null;request=null;status('正在本地识别，请稍候…');
 try{const result=await api('/api/upload',file,true);imageId=result.image_id;draft=result.draft;$('apply-draft').hidden=!draft||!!correction;$('preview').src='/api/image/'+imageId;$('image-panel').hidden=false;$('ocr').value=result.rows.map(r=>r.text).join('\n');$('image-status').textContent=(result.duplicate?'这张截图已上传过。请检查是否已有对应记录。':'截图已保存在本机。')+(result.error||'识别文字仅供核对，尚未入账。');status('请对照截图填写下方字段，然后确认保存。');}
 catch(e){status(e.message);}finally{busy=false;$('save').disabled=false;}
};
function number(id) { if(!$(id).value.trim())throw new Error('请填写数量和价格');return Number($(id).value); }
function dataFor(kind){
 const notes=$('notes').value.trim();if(!notes)throw new Error('请填写备注或当时理由');
 if(kind==='execution'){const data={symbol:$('symbol').value.trim().toUpperCase(),side:$('side').value,quantity:number('quantity'),price:number('price'),currency:'CNY',notes};const external=$('external-id').value.trim();if(external){data.external_id=external;data.external_source='gtht:personal';}return data;}
 if(kind==='snapshot'){const positions=$('positions').value.trim().split('\n').filter(x=>x.trim()).map(line=>{const parts=line.trim().split(/[\s,，]+/);if(parts.length!==2||!/^\d+(\.\d+)?$/.test(parts[1]))throw new Error('持仓每行需为：股票代码 数量');return {symbol:parts[0].toUpperCase(),quantity:Number(parts[1])};});return {positions,cash:$('cash').value===''?null:Number($('cash').value),complete:$('complete').checked,notes};}
 if(kind==='intent')return {action:$('action').value,reason:notes};
 return {thesis:notes,rules:$('rules').value.split('\n').map(x=>x.trim()).filter(Boolean),confirmed:true,confirmation_quote:notes};
}
$('entry').onsubmit=async event=>{
 event.preventDefault();if(busy)return;busy=true;$('save').disabled=true;
 try{const kind=$('kind').value;let data=dataFor(kind);let payload={kind,data,confirmed:$('confirmed').checked,image_id:imageId};
 if(correction){payload.kind='correction';payload.parent_id=correction.id;payload.data={reason:'用户在网页核对后更正：'+$('notes').value,replacement:data};}
 else if(kind!=='plan')payload.occurred_at=new Date($('when').value).toISOString();
 const serialized=JSON.stringify(payload);
 // Exact retry uses the original key. Screenshot-derived records additionally dedupe
 // identical content across refreshes; broker execution IDs dedupe across images.
 if(!request||request.serialized!==serialized){
   if(kind==='plan'&&!correction){let after=0,parent=null;do{const page=await api('/api/history?after='+after);for(const e of page.events)if(e.kind==='plan')parent=e.id;after=page.next_after;}while(after!==null);if(parent)payload.parent_id=parent;}
   request={serialized,key:'web:'+Date.now()+':'+Math.random().toString(36).slice(2),payload};
 }
 payload=request.payload;
 payload.request_key=request.key;const result=await api('/api/events',payload);
 status((result.replayed?'这条内容已保存，未重复入账。':'已保存。')+' 记录编号：'+result.event.id);$('confirmed').checked=false;await loadOverview();
 if(correction)resetCorrection();
 }catch(e){status(e.message);}finally{busy=false;$('save').disabled=false;}
};
function resetCorrection(){correction=null;$('kind').disabled=false;$('cancel-correction').hidden=true;$('save').textContent='确认保存';clearImage();fields();}
$('cancel-correction').onclick=resetCorrection;
function edit(event){
 const kind=event.corrects_kind||event.kind;const data=event.data.replacement||event.data;
 if(data.statement_sha256||data.market_value!=null){status('这条导入记录含原始费用或估值字段。请提供记录编号与更正内容交给助手核对，避免表单覆盖原始信息。');return;}
 if(!['execution','snapshot','intent'].includes(kind)){status('计划请通过新计划修订；此类记录暂不支持表单更正。');return;}
 correction=event;clearImage();$('kind').value=kind;$('kind').disabled=true;$('cancel-correction').hidden=false;$('save').textContent='确认追加更正';$('confirmed').checked=false;
 $('notes').value=data.notes||data.reason||'';$('symbol').value=data.symbol||'';$('side').value=data.side||'buy';$('quantity').value=data.quantity??'';$('price').value=data.price??'';$('external-id').value=data.external_id||'';$('positions').value=(data.positions||[]).map(p=>p.symbol+' '+p.quantity).join('\n');$('cash').value=data.cash??'';$('complete').checked=!!data.complete;$('action').value=data.action||'watch';fields();tab('capture');status('正在更正 '+event.id+'。原始记录会保留；请说明更正后的内容。');
}
function displayData(event){
 const data=event.data.replacement||event.data, kind=event.corrects_kind||event.kind;
 const action={buy:'买入',sell:'卖出',add:'加仓',reduce:'减仓',watch:'观察',defer:'暂缓',cancel:'取消'};
 let lines=[];
 if(kind==='execution')lines=[data.symbol+' · '+(action[data.side]||data.side),data.quantity+' 股 × '+data.price+' 元',data.external_id?'成交编号：'+data.external_id:'成交编号：未知','备注：'+(data.notes||'未填写')];
 else if(kind==='snapshot')lines=[data.complete?'完整持仓快照':'局部持仓快照（不能代表全账户）',...(data.positions||[]).map(p=>p.symbol+'：'+p.quantity+' 股'),'可用资金：'+(data.cash==null?'未知':data.cash+' 元'),'备注：'+data.notes];
 else if(kind==='account_activity')lines=[data.activity,'资金发生：'+data.cash_change+' 元',data.name||'',data.notes||''];
 else if(kind==='intent')lines=['考虑操作：'+(action[data.action]||data.action),'当时理由：'+data.reason];
 else if(kind==='plan')lines=['计划：'+data.thesis,...(data.rules||[]).map(r=>'• '+r),data.confirmed?'已确认':'草稿'];
 else lines=[data.assessment||data.observation||data.reason||'请查看证据摘要'];
 if(data.price_amount_mismatch)lines.push('注意：原单显示单价 × 数量与成交金额不一致，保留原单金额 '+data.gross_amount+' 元。');
 if(event.kind==='correction')lines.unshift('更正理由：'+event.data.reason);
 return lines.join('\n');
}
async function loadHistory(append=false){
 const result=await api('/api/history?after='+(append?nextAfter:0));if(!append)$('history').replaceChildren();
 for(const event of result.events){const card=document.createElement('article');card.className='record';const title=document.createElement('h3');title.textContent=(names[event.kind]||event.kind)+' · '+(event.data.settlement_date?'交收日 '+event.data.settlement_date+'（未提供成交时刻）':new Date(event.occurred_at).toLocaleString());const id=document.createElement('small');id.textContent=event.id;const pre=document.createElement('pre');pre.textContent=displayData(event);card.append(title,id,pre);
 if(event.parent_id){const parent=document.createElement('p');parent.textContent='关联原记录：'+event.parent_id;card.append(parent);}
 if(event.source_ref.startsWith('screenshot:')){const link=document.createElement('a');link.href='/api/image/'+event.source_ref.slice(11);link.textContent='查看来源截图';link.target='_blank';link.rel='noopener';card.append(link);}
 const match=/^statement:([a-f0-9]{64}):p(\d+):r(\d+)$/.exec(event.source_ref);if(match){const link=document.createElement('a');link.href='/api/statement/'+match[1]+'#page='+match[2];link.textContent='查看原单第 '+match[2]+' 页，第 '+match[3]+' 行';link.target='_blank';link.rel='noopener';card.append(link);}
 const button=document.createElement('button');button.textContent='更正这条记录';button.onclick=()=>edit(event);if(['execution','snapshot','intent'].includes(event.corrects_kind||event.kind))card.append(button);$('history').append(card);}
 nextAfter=result.next_after;$('more').hidden=nextAfter===null;if(!$('history').children.length)$('history').textContent='还没有记录。从一张截图或一个想法开始。';
}
$('refresh').onclick=()=>loadHistory().catch(e=>status(e.message));$('more').onclick=()=>loadHistory(true).catch(e=>status(e.message));
$('make-report').onclick=async()=>{try{$('report').value=(await api('/api/report')).text;status('已生成本地证据摘要，没有调用模型。');}catch(e){status(e.message);}};
$('copy-question').onclick=async()=>{try{$('report').value=(await api('/api/report')).text;const text='请基于以下个人记录分析，引用记录编号，区分事实、推断和未知；不要推断未记录的动机，不要执行交易。\n\n问题：'+$('question').value+'\n\n'+$('report').value;try{await navigator.clipboard.writeText(text);status('已复制，可粘贴到当前助手对话。');}catch{$('report').value=text;$('report').focus();$('report').select();status('请长按选中并复制文本。');}}catch(e){status(e.message);}};
api('/api/history').then(()=>unlock()).catch(()=>{});
