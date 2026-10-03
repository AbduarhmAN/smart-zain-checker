/* Local preview effects. No Telegram requests or changes to the checker. */
const TelegramMotion = {
  revision:0,
  animations:new Set(),
  preferred:true,
  media:window.matchMedia('(prefers-reduced-motion: reduce)'),
  easing:'cubic-bezier(.22,.75,.25,1)',
  get enabled(){return this.preferred && !this.media.matches;},

  init(){
    try{this.preferred=localStorage.getItem('zc-preview-motion')!=='off';}catch{}
    const tag=document.querySelector('.preview-tag');
    const controls=document.createElement('div');controls.className='motion-controls';
    tag.before(controls);controls.append(tag);
    const toggle=document.createElement('button');toggle.id='motionToggle';toggle.className='motion-toggle';
    toggle.type='button';controls.append(toggle);
    toggle.onclick=()=>{this.preferred=!this.preferred;try{localStorage.setItem('zc-preview-motion',this.preferred?'on':'off');}catch{}this.sync();};
    this.media.addEventListener('change',()=>this.sync());this.sync();
    document.addEventListener('click',event=>{
      const button=event.target.closest('button');if(!button || !this.enabled || button.id==='motionToggle')return;
      const box=button.getBoundingClientRect();const wave=document.createElement('span');wave.className='tap-wave';wave.setAttribute('aria-hidden','true');
      wave.style.left=(event.detail?event.clientX-box.left:box.width/2)-6+'px';
      wave.style.top=(event.detail?event.clientY-box.top:box.height/2)-6+'px';button.append(wave);
      const spread=Math.max(box.width,box.height)/6;
      const animation=this.animate(wave,[{transform:'scale(0)',opacity:.2},{transform:'scale('+spread+')',opacity:0}],{duration:450});
      if(animation)animation.finished.then(()=>wave.remove(),()=>wave.remove());else wave.remove();
    },true);
    this.animate(document.querySelector('.phone'),[{opacity:0,transform:'translateY(22px)'},{opacity:1,transform:'translateY(0)'}],{duration:650});
    [...document.querySelector('.intro').children].forEach((el,index)=>this.animate(el,[{opacity:0,transform:'translateY(12px)'},{opacity:1,transform:'translateY(0)'}],{duration:500,delay:70+index*55}));
  },

  sync(){
    document.body.classList.toggle('motion-off',!this.enabled);
    const toggle=document.getElementById('motionToggle');
    toggle.setAttribute('aria-pressed',String(this.enabled));toggle.disabled=this.media.matches;
    toggle.textContent=this.media.matches?'الحركة مخفّضة':this.enabled?'إيقاف الحركة':'تفعيل الحركة';
    toggle.title=this.media.matches?'تُحترم إعدادات تقليل الحركة في جهازك':'التحكم في مؤثرات المعاينة';
    if(!this.enabled)this.animations.forEach(animation=>animation.cancel());
  },

  animate(element,frames,options={}){
    if(!this.enabled || !element?.animate)return null;
    const animation=element.animate(frames,{duration:300,easing:this.easing,fill:'backwards',...options});
    this.animations.add(animation);
    animation.finished.then(()=>this.animations.delete(animation),()=>this.animations.delete(animation));
    return animation;
  },

  progress(){
    const code=document.querySelector('#messageBody code');const match=code?.textContent.match(/(\d+)%/);
    if(!match)return;
    const percentage=Math.min(100,Math.max(0,Number(match[1])));
    code.className='motion-progress';code.textContent=percentage+'%';
    code.setAttribute('role','progressbar');code.setAttribute('aria-label','تقدم المعالجة في المعاينة');
    code.setAttribute('aria-valuemin','0');code.setAttribute('aria-valuemax','100');code.setAttribute('aria-valuenow',percentage);
    const track=document.createElement('span');track.className='motion-track';track.setAttribute('aria-hidden','true');
    const fill=document.createElement('span');fill.className='motion-fill';fill.style.transform='scaleX('+(percentage/100)+')';
    track.append(fill);code.append(track);
    this.animate(fill,[{transform:'scaleX(0)'},{transform:fill.style.transform}],{duration:800,delay:100});
  },

  async change(render){
    const revision=++this.revision;
    const message=document.querySelector('.message');const buttons=document.getElementById('inlineButtons');
    const restoreFocus=buttons.contains(document.activeElement);
    this.animations.forEach(animation=>{if([message,buttons].includes(animation.effect?.target))animation.cancel();});
    const exit=this.animate(message,[{opacity:1,transform:'translateY(0)'},{opacity:0,transform:'translateY(-5px)'}],{duration:100});
    if(exit){try{await exit.finished;}catch{}}
    if(revision!==this.revision)return;
    render();this.progress();
    this.animate(message,[{opacity:0,transform:'translateY(10px)'},{opacity:1,transform:'translateY(0)'}],{duration:320});
    [...buttons.children].forEach((row,index)=>this.animate(row,[{opacity:0,transform:'translateY(7px)'},{opacity:1,transform:'translateY(0)'}],{duration:260,delay:60+index*40}));
    if(restoreFocus)buttons.querySelector('button')?.focus({preventScroll:true});
  }
};
