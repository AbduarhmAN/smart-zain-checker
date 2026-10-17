'use strict';
window.CampaignMotion=class{
 constructor(canvas,{onUpdate=()=>{},loop=false,onComplete=()=>{},videoId='overview'}={}){this.canvas=canvas;this.videoId=videoId;this.onUpdate=onUpdate;this.onComplete=onComplete;this.duration=CampaignArt.duration;this.state={seconds:0};this.loop=loop;this.playing=false;this.recording=false;this.draw();this.timeline=gsap.timeline({paused:true,repeat:loop?-1:0,onComplete:()=>{this.playing=false;onComplete();}}).fromTo(this.state,{seconds:0},{seconds:this.duration,duration:this.duration,ease:'none',immediateRender:false,onUpdate:()=>this.draw()});}
 draw(){CampaignArt.film(this.canvas,this.state.seconds,this.videoId);this.onUpdate(this.state.seconds);}
 setVideo(id){if(this.recording)throw new Error('انتظر اكتمال التصدير.');this.pause();this.videoId=id;this.seek(0);}
 play(){if(this.state.seconds>=this.duration-.01)this.seek(0);this.timeline.play();this.playing=true;}
 pause(){this.timeline.pause();this.playing=false;}
 seek(time){const position=Math.min(this.duration,Math.max(0,time));this.timeline.time(position,true);this.state.seconds=position;this.draw();}
 restart(){this.timeline.restart();this.playing=true;}
 async exportVideo(progress=()=>{}){
  if(!window.MediaRecorder || !this.canvas.captureStream)throw new Error('هذا المتصفح لا يدعم حفظ الفيديو. استخدم Chrome أو Edge حديثًا.');
  if(this.recording)throw new Error('يجري حفظ الفيديو بالفعل.');
  const formats=['video/mp4;codecs=avc1.42001E','video/webm;codecs=vp9','video/webm;codecs=vp8','video/webm'];
  const mimeType=formats.find(type=>MediaRecorder.isTypeSupported(type));if(!mimeType)throw new Error('لا يتوفر تنسيق فيديو مدعوم في هذا المتصفح.');
  this.pause();this.seek(0);this.recording=true;
  const stream=this.canvas.captureStream(30),chunks=[];
  // captureStream contains video tracks only. No microphone/audio is requested.
  const recorder=new MediaRecorder(stream,{mimeType,videoBitsPerSecond:9000000});
  const blobPromise=new Promise((resolve,reject)=>{recorder.ondataavailable=e=>{if(e.data.size)chunks.push(e.data);};recorder.onerror=e=>reject(e.error||new Error('تعذر تسجيل الفيديو'));recorder.onstop=()=>resolve(new Blob(chunks,{type:mimeType}));});
  const previousUpdate=this.onUpdate;
  this.onUpdate=time=>{previousUpdate(time);progress(time/this.duration);};
  recorder.start(500);
  try{
   await new Promise((resolve,reject)=>{this.recordTimer=setTimeout(()=>reject(new Error('توقف التصدير. أبقِ الصفحة ظاهرة وحاول مجددًا.')),50000);this.exportTimeline=gsap.timeline({onComplete:resolve}).fromTo(this.state,{seconds:0},{seconds:this.duration,duration:this.duration,ease:'none',immediateRender:false,onUpdate:()=>this.draw()});});
   recorder.stop();const blob=await blobPromise;
   return {blob,extension:mimeType.startsWith('video/mp4')?'mp4':'webm'};
  }finally{
   clearTimeout(this.recordTimer);this.exportTimeline?.kill();if(recorder.state!=='inactive')recorder.stop();stream.getTracks().forEach(track=>track.stop());this.onUpdate=previousUpdate;this.recording=false;this.timeline.time(this.duration,true);this.state.seconds=this.duration;this.draw();
  }
 }
};
window.campaignFonts=async()=>{await Promise.all([document.fonts.load('700 96px Tajawal'),document.fonts.load('400 40px Tajawal'),document.fonts.load('500 40px Tajawal')]);};
window.campaignToast=message=>{const node=document.getElementById('toast');node.textContent=message;node.classList.add('visible');clearTimeout(window.campaignToastTimer);window.campaignToastTimer=setTimeout(()=>node.classList.remove('visible'),4300);};
window.copyCampaignText=async(text,message='تم النسخ. أرسلها في ماسنجر صفحتنا.')=>{try{await navigator.clipboard.writeText(text);campaignToast(message);}catch{campaignToast('انسخ النص يدويًا: '+text);}};
window.saveCampaignFile=async(blob,name)=>{
 // The isolated local preview also saves a review copy into exports/.
 if(location.hostname==='127.0.0.1' && location.port==='5062'){
  const response=await fetch('/__studio/export?name='+encodeURIComponent(name),{method:'POST',headers:{'Content-Type':'application/octet-stream'},body:blob});
  if(!response.ok)throw new Error('تعذر حفظ نسخة الملف في مجلد الحملة.');
 }
 const url=URL.createObjectURL(blob),link=document.createElement('a');link.href=url;link.download=name;document.body.append(link);link.click();link.remove();setTimeout(()=>URL.revokeObjectURL(url),60000);
};
