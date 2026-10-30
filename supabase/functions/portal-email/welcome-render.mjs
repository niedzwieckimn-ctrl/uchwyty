import {welcomeHtml,welcomeText} from './welcome-template.mjs';
import {welcomeAttachments} from './welcome-assets.mjs';
export const WELCOME_SUBJECT='Witamy w programie Niedźwieccy dla Architektów!';
export const DEFAULT_PANEL_URL='https://niedzwieccy-architekci.netlify.app/';
export function renderWelcomeEmail(panelUrl=DEFAULT_PANEL_URL) {
  const url=new URL(panelUrl);
  if(url.protocol!=='https:'||url.username||url.password)throw new Error('INVALID_ARCHITECT_PANEL_URL');
  const escaped=url.href.replaceAll('&','&amp;').replaceAll('"','&quot;').replaceAll('<','&lt;').replaceAll('>','&gt;');
  return {subject:WELCOME_SUBJECT,html:welcomeHtml.replaceAll('{{PANEL_URL}}',escaped),
    text:welcomeText.replaceAll('{{PANEL_URL}}',url.href),attachments:structuredClone(welcomeAttachments)};
}
export function prepareWelcomeEmail(rpc,panelUrl=DEFAULT_PANEL_URL) {
  return async(job,owner)=>{
    if(job.request?._template!=='architect-approved-v1')return job.request;
    const rendered=renderWelcomeEmail(panelUrl);
    return rpc('portal_email_freeze_welcome',{p_id:job.id,p_owner:owner,
      p_html:rendered.html,p_text:rendered.text,p_attachments:rendered.attachments});
  };
}
