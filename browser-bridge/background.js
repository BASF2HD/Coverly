const validToken = t => typeof t==='string' && /^[A-Za-z0-9_-]{40,100}$/.test(t);
const originOf = u => {try{return new URL(u).origin;}catch{return '';}};
chrome.runtime.onMessage.addListener((message,sender,reply)=>{
 (async()=>{
  if(message.type==='coverly-metadata-start'){
   if(originOf(sender.url)!=='http://localhost:8000'||!sender.tab||!(/^[a-f0-9]{32}$/).test(message.album_id)||typeof message.title!=='string'||message.title.length>300)throw Error('Invalid album connection');
   await chrome.storage.session.set({'coverly-metadata-target':{albumId:message.album_id,title:message.title,sourceTab:sender.tab.id,expires:Date.now()+1800000}});
   if(message.open_search)await chrome.tabs.create({url:'https://www.google.com/search?q='+encodeURIComponent(message.title+' Tamil movie music composer soundtrack singers')});
   return {ok:true};
  }
  if(message.type==='coverly-start'){
   if(originOf(sender.url)!=='http://localhost:8000'||!sender.tab||!validToken(message.token))throw Error('Invalid Coverly handoff');
   const tab=await chrome.tabs.create({url:'https://chatgpt.com/#coverly-transfer='+encodeURIComponent(message.token)});
   await chrome.storage.session.set({['coverly-tab-'+tab.id]:{token:message.token,sourceTab:sender.tab.id,expires:Date.now()+600000}});
   return {ok:true};
  }
  if(originOf(sender.url)!=='https://chatgpt.com'||!sender.tab)throw Error('ChatGPT tab required');
  const key='coverly-tab-'+sender.tab.id,entry=(await chrome.storage.session.get(key))[key];
  if(!entry||entry.expires<Date.now())throw Error('This tab has no current Coverly handoff');
  if(message.type==='coverly-has-transfer')return {pending:true};
  if(message.type==='coverly-bundle'){
   const response=await fetch('http://localhost:8000/api/chatgpt-transfers/'+encodeURIComponent(entry.token),{cache:'no-store'});
   if(!response.ok)throw Error((await response.json()).detail||'Coverly is unavailable');
   return await response.json();
  }
  if(message.type==='coverly-status'){
   await chrome.tabs.sendMessage(entry.sourceTab,{type:'coverly-transfer-status',token:entry.token,status:message.status,message:message.message}).catch(()=>{});
   if(message.status==='offered')await chrome.storage.session.remove(key);
   return {ok:true};
  }
  throw Error('Unknown handoff action');
 })().then(reply,error=>reply({error:error.message}));
 return true;
});

chrome.action?.onClicked?.addListener(()=>chrome.tabs.create({url:'http://localhost:8000/#studio'}));
chrome.runtime.onInstalled?.addListener(info=>{if(info.reason==='install')chrome.tabs.create({url:'http://localhost:8000/#studio'});});
