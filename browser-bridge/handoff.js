/* No remote scripts, account cookies, Send clicks, or uploads beyond selected files. */
if(location.origin==='http://localhost:8000'){
 document.documentElement.dataset.coverlyBridge='1';document.documentElement.dataset.coverlyMetadataBridge='1';window.dispatchEvent(new Event('coverly-bridge-ready'));
 window.addEventListener('message',event=>{if(event.source!==window||event.origin!==location.origin||event.data?.type!=='coverly-metadata-connect')return;chrome.runtime.sendMessage({type:'coverly-metadata-start',album_id:event.data.album_id,title:event.data.title,open_search:event.data.open_search}).then(result=>window.postMessage({type:'coverly-metadata-status',...result},location.origin));});
 chrome.runtime.onMessage.addListener((message,sender,reply)=>{if(message.type==='coverly-resolve-album'){const button=document.querySelector('[data-action=lookup-artist]'),title=document.querySelector('#lookup-title');reply(button&&document.querySelector('#modal')?.open?{albumId:button.dataset.id,title:title?.value||document.title}:null);return;}if(message.type!=='coverly-page-metadata')return;fetch('/api/albums/'+encodeURIComponent(message.album_id)+'/metadata-page',{method:'POST',headers:{'X-Coverly-Request':'1','Content-Type':'application/json'},body:JSON.stringify({...message.page,title:message.title,allow_title_mismatch:true})}).then(async response=>{const data=await response.json();if(!response.ok)throw Error(data.detail||'Import failed');window.postMessage({type:'coverly-metadata-received',album_id:message.album_id},location.origin);reply({ok:true});}).catch(error=>reply({error:error.message}));return true;});
 window.addEventListener('message',event=>{
  if(event.source!==window||event.origin!==location.origin||event.data?.type!=='coverly-transfer')return;
  chrome.runtime.sendMessage({type:'coverly-start',token:event.data.token}).then(result=>window.postMessage({type:'coverly-transfer-status',token:event.data.token,status:result?.error?'failed':'opening',message:result?.error||'Opening ChatGPT with selected artwork'},location.origin)).catch(error=>window.postMessage({type:'coverly-transfer-status',token:event.data.token,status:'failed',message:error.message},location.origin));
 });
 chrome.runtime.onMessage.addListener(message=>{if(message.type==='coverly-transfer-status')window.postMessage(message,location.origin);});
}else if(location.origin==='https://chatgpt.com'){
 (async()=>{
 const pending=await chrome.runtime.sendMessage({type:'coverly-has-transfer'}).catch(()=>null);
 if(!location.hash.startsWith('#coverly-transfer=')&&!pending?.pending)return;
 let busy=false;
 const banner=document.createElement('section');banner.id='coverly-handoff-banner';
 Object.assign(banner.style,{position:'fixed',top:'10px',right:'16px',zIndex:2147483647,background:'#20301d',color:'white',padding:'16px',borderRadius:'10px',maxWidth:'410px',font:'14px/1.6 system-ui',boxShadow:'0 4px 25px #0008'});
 const label=document.createElement('div');label.textContent='Coverly: waiting for the ChatGPT attachment control. Sign in if required.';
 const button=document.createElement('button');button.textContent='Attach selected Coverly images';button.style.cssText='padding:8px;margin-top:10px;cursor:pointer;color:black;background:#dbef86;border:0;border-radius:5px';
 const close=document.createElement('button');close.textContent='×';close.setAttribute('aria-label','Close Coverly handoff notice');close.style.cssText='float:right;background:none;color:white;border:0;font-size:20px';close.onclick=()=>banner.remove();
 banner.append(close,label,button);document.body.append(banner);
 async function attach(){
  if(busy)return false;
  const input=[...document.querySelectorAll('input[type=file]')].find(el=>!el.disabled&&(el.multiple||/image/i.test(el.accept)));
  const composer=document.querySelector('#prompt-textarea,[role=textbox][contenteditable=true],textarea');
  if(!input||!composer)return false;
  const draft='value' in composer?composer.value:composer.textContent;
  if(draft?.trim()){label.textContent='Your current draft has text. Open a new empty ChatGPT draft, then retry; Coverly will not overwrite it.';return false;}
  busy=true;button.disabled=true;
  try{
   const payload=await chrome.runtime.sendMessage({type:'coverly-bundle'});
   if(payload.error)throw Error(payload.error);
   const transfer=new DataTransfer();
   for(const item of payload.files){const bytes=Uint8Array.from(atob(item.data),c=>c.charCodeAt(0));transfer.items.add(new File([bytes],item.name,{type:item.mimeType}));}
   input.files=transfer.files;input.dispatchEvent(new Event('change',{bubbles:true}));
   composer.focus();
   if('value' in composer){const setter=Object.getOwnPropertyDescriptor(Object.getPrototypeOf(composer),'value')?.set;if(setter)setter.call(composer,payload.prompt);else composer.value=payload.prompt;composer.dispatchEvent(new Event('input',{bubbles:true}));}
   else document.execCommand('insertText',false,payload.prompt);
   label.textContent='Selected files were handed to ChatGPT’s upload control. Wait for the attachment thumbnails, check the prompt, then press Send yourself. If the site rejects the upload, use Copy image in Coverly and paste here.';
   button.remove();history.replaceState(null,'',location.pathname+location.search);
   await chrome.runtime.sendMessage({type:'coverly-status',status:'offered',message:'Images handed to ChatGPT’s attachment control. Verify thumbnails there before sending.'}).catch(()=>{});
   return true;
  }catch(error){label.textContent='Coverly: '+error.message;busy=false;button.disabled=false;return false;}
 }
 button.onclick=attach;
 let attempts=0;const timer=setInterval(async()=>{if(await attach()||++attempts>120)clearInterval(timer);},1000);
 })();
}
