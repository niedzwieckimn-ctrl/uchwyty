/* Only an explicit administrator submit wakes the worker. Polling is read-only. */
(()=>{
 const form=document.querySelector('[data-welcome-send]');
 if(!form)return;
 const status=document.querySelector('[data-welcome-status]'),button=form.querySelector('button[type=submit]');
 let running=false,controller;
 const labels={sending:'Wysyłanie',sent:'Wysłano',error:'Błąd wysyłki',queued:'W kolejce',delivered:'Dostarczono'};
 const show=(state,extra='')=>{status.textContent=(labels[state]||'Nie udało się sprawdzić statusu')+(extra?' — '+extra:'')};
 async function poll(job){
  for(let attempt=0;attempt<20;attempt++){
   await new Promise(r=>setTimeout(r,1500));
   if(controller.signal.aborted)return;
   const r=await fetch(form.dataset.statusUrl+'?job='+encodeURIComponent(job),{headers:{Accept:'application/json'},cache:'no-store',signal:controller.signal});
   if(!r.ok)throw Error('status');const result=await r.json();
   show(result.state,result.requires_review?'wymaga sprawdzenia poprzedniej próby':result.queued&&result.state==='error'?'zaplanowano ponowienie':'');
   if(['sent','delivered'].includes(result.state)){
    form.elements.request_id.value=crypto.randomUUID();return;
   }
   if(result.state==='error'||result.state==='queued'&&attempt>=3)return;
  }
 }
 form.addEventListener('submit',async event=>{
  event.preventDefault();if(running)return;
  running=true;button.disabled=true;controller=new AbortController();show('sending');
  try{
   const response=await fetch(form.action,{method:'POST',body:new FormData(form),headers:{Accept:'application/json'},signal:controller.signal});
   if(!response.ok){show('error','nie udało się rozpocząć próby; odśwież kartę');return}
   const result=await response.json();show(result.state);await poll(result.job_id);
  }catch(error){if(error.name!=='AbortError')status.textContent='Nie udało się potwierdzić statusu. Odśwież kartę przed ponowną próbą.'}
  finally{running=false;button.disabled=false}
 });
 window.addEventListener('pagehide',()=>controller?.abort(),{once:true});
 if(status.dataset.job&&['sending','queued'].includes(status.dataset.state)){
  controller=new AbortController();running=true;button.disabled=true;
  poll(status.dataset.job).catch(()=>{status.textContent='Nie udało się potwierdzić statusu. Odśwież kartę.'}).finally(()=>{running=false;button.disabled=false});
 }
})();
