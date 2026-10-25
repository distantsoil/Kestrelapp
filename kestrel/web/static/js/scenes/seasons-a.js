/* Kestrel seasonal touches, set A (group: Seasons and nature). Overlays for viewBox 0 0 560 110. */
window.SEASONS=window.SEASONS||[];
(function(){
const f1=n=>Math.round(n*10)/10;
const rnd=i=>{const x=Math.sin(i*127.1+311.7)*43758.5453;return x-Math.floor(x);};
const sty=css=>`<style>@media (prefers-reduced-motion:no-preference){${css}}</style>`;
/* Particles that drift/fall once across the band. Each gets its own start (static pose when motion is off). */
function fall(id,n,o){
  let s='';
  for(let i=0;i<n;i++){
    const x=o.x0+rnd(i+o.s)*(o.x1-o.x0),y=o.y0+rnd(i+o.s+40)*o.yr;
    const dx=-(o.dx0+rnd(i+o.s+80)*o.dxr),dy=o.dy0+rnd(i+o.s+120)*o.dyr;
    const rot=Math.round(o.rot*(rnd(i+o.s+160)>.5?1:-1)),dur=f1(o.d0+rnd(i+o.s+200)*o.dr),dl=f1(-rnd(i+o.s+240)*dur);
    s+=`<g transform="translate(${f1(x)} ${f1(y)})"><g class="ss-${id}-f" style="--dx:${f1(dx)}px;--dy:${f1(dy)}px;--r:${rot}deg;animation-duration:${dur}s;animation-delay:${dl}s"><g class="ss-${id}-sw" style="animation-delay:${f1(-rnd(i+o.s+280)*4)}s">${o.shape(i)}</g></g></g>`;
  }
  return s;
}
const fallCss=(id,sw)=>`.ss-${id}-f{animation:ss-${id}-fl linear infinite}@keyframes ss-${id}-fl{0%{opacity:0;transform:translate(0,0) rotate(0)}12%{opacity:1}85%{opacity:1}100%{opacity:0;transform:translate(var(--dx),var(--dy)) rotate(var(--r))}}.ss-${id}-sw{animation:ss-${id}-sy 3.4s ease-in-out infinite alternate;transform-box:fill-box;transform-origin:center}@keyframes ss-${id}-sy{from{transform:translateX(-5px)${sw?' scaleX(1)':''}}to{transform:translateX(5px)${sw?' scaleX(.45)':''}}}`;

/* 1. Spring: blossom petals */
SEASONS.push({id:'spring',name:'Spring',group:'Seasons and nature',when:'Around March 20',moves:'Blossom petals drift across the band',draw(){
  const shape=i=>{const c=i%3?'var(--z1)':'var(--snow)';return `<path d="M0 -3.2Q3 -1.2 0 3.2Q-3 -1.2 0 -3.2Z" fill="${c}" opacity="${i%3?.95:.85}"/><path d="M0 -3.2Q1.3 0 0 2.6" stroke="var(--paper)" stroke-width=".4" fill="none" opacity=".5"/>`;};
  return sty(fallCss('spring',true))+fall('spring',18,{s:1,x0:210,x1:600,y0:-6,yr:22,dx0:80,dxr:70,dy0:62,dyr:26,rot:260,d0:13,dr:7,shape});
}});

/* 2. Earth Day: seedlings and a pair of birds */
SEASONS.push({id:'earthday',name:'Earth Day',group:'Seasons and nature',when:'Around April 22',moves:'Seedlings sprout along the edge, two birds cross',draw(){
  let o=sty(`.ss-earthday-sd{animation:ss-earthday-gr 12s ease-in-out infinite;transform-box:fill-box;transform-origin:50% 100%}@keyframes ss-earthday-gr{0%{transform:scale(.05);opacity:0}8%{opacity:1}38%,88%{transform:scale(1);opacity:1}100%{transform:scale(.9);opacity:0}}.ss-earthday-lf{animation:ss-earthday-sw 3.6s ease-in-out infinite alternate;transform-box:fill-box;transform-origin:50% 100%}@keyframes ss-earthday-sw{from{transform:rotate(-6deg)}to{transform:rotate(7deg)}}.ss-earthday-br{animation:ss-earthday-fl 26s linear infinite}@keyframes ss-earthday-fl{from{transform:translate(640px,0)}to{transform:translate(120px,12px)}}.ss-earthday-wg{animation:ss-earthday-fp 1.6s ease-in-out infinite alternate;transform-box:fill-box;transform-origin:center}@keyframes ss-earthday-fp{to{transform:scaleY(.5)}}`);
  [[250,0],[318,-4],[392,-8],[470,-2],[536,-6]].forEach(([x,d],i)=>{
    o+=`<g transform="translate(${x} 110)"><g class="ss-earthday-sd" style="animation-delay:${d}s"><g class="ss-earthday-lf" style="animation-delay:${-i*.7}s"><path d="M0 0Q.8 -5 0 -9" stroke="var(--z3)" stroke-width="1.3" fill="none" stroke-linecap="round"/><path d="M0 -6Q-6 -6.5 -7.5 -11Q-1 -11.5 0 -6Z" fill="var(--z3)"/><path d="M0 -8.5Q5 -9 7 -13.5Q1.5 -14 0 -8.5Z" fill="var(--z3)" opacity=".85"/></g></g></g>`;});
  const bird=`<path class="ss-earthday-wg" d="M0 0q3 -3.4 6 0q3 -3.4 6 0" fill="none" stroke="var(--bird)" stroke-width="1.4" stroke-linecap="round"/>`;
  o+=`<g transform="translate(0 28)"><g class="ss-earthday-br">${bird}<g transform="translate(15 6) scale(.8)">${bird}</g></g></g>`;
  return o;
}});

/* 3. Longest day: wide warm glow and butterflies */
SEASONS.push({id:'longday',name:'Longest day',group:'Seasons and nature',when:'Around June 21',moves:'A wide warm glow swells, butterflies wander',draw(){
  const bf=(c1,c2)=>`<g class="ss-longday-wg"><path d="M0 0C-2 -4 -6 -4.5 -5.5 -1.2C-5 1 -2 1.2 0 0ZM0 0C-1.5 1.5 -4 3.8 -2.5 5C-1 5.4 0 2.5 0 0Z" fill="${c1}"/><path d="M0 0C2 -4 6 -4.5 5.5 -1.2C5 1 2 1.2 0 0ZM0 0C1.5 1.5 4 3.8 2.5 5C1 5.4 0 2.5 0 0Z" fill="${c2}"/></g><rect x="-.4" y="-2.4" width=".8" height="6" rx=".4" fill="var(--bird)"/>`;
  let o=sty(`.ss-longday-gl{animation:ss-longday-br 8s ease-in-out infinite;transform-box:fill-box;transform-origin:center}@keyframes ss-longday-br{0%,100%{opacity:.55;transform:scale(.92)}50%{opacity:1;transform:scale(1.12)}}.ss-longday-bt{animation:ss-longday-wd 16s ease-in-out infinite}@keyframes ss-longday-wd{0%,100%{transform:translate(0,0)}25%{transform:translate(-38px,-9px)}50%{transform:translate(-72px,8px)}75%{transform:translate(-34px,12px)}}.ss-longday-wg{animation:ss-longday-fp 1.5s ease-in-out infinite alternate;transform-box:fill-box;transform-origin:center}@keyframes ss-longday-fp{from{transform:scaleX(1)}to{transform:scaleX(.35)}}`);
  o+=`<defs><radialGradient id="ss-longday-rg"><stop offset="0" stop-color="var(--lite)" stop-opacity=".38"/><stop offset=".4" stop-color="var(--glow)" stop-opacity=".26"/><stop offset="1" stop-color="var(--glow)" stop-opacity="0"/></radialGradient></defs>`;
  o+=`<circle class="ss-longday-gl" cx="415" cy="38" r="104" fill="url(#ss-longday-rg)"/>`;
  [[520,44,'var(--z2)','var(--z1)',0,.9],[300,56,'var(--z1)','var(--z2)',-5,.8],[450,66,'var(--z2)','var(--lite)',-9,.85]].forEach(([x,y,a,b,d,sc])=>{
    o+=`<g transform="translate(${x} ${y}) scale(${sc})"><g class="ss-longday-bt" style="animation-delay:${d}s">${bf(a,b)}</g></g>`;});
  return o;
}});

/* 4. Shooting stars */
SEASONS.push({id:'meteors',name:'Shooting stars',group:'Seasons and nature',when:'Around August 12',moves:'Meteors streak across the sky every few seconds',draw(){
  let o=sty(`.ss-meteors-m{animation:ss-meteors-go 9s linear infinite;opacity:0}@keyframes ss-meteors-go{0%,62%{opacity:0;transform:translate(0,0)}64%{opacity:1}74%{opacity:0;transform:translate(-120px,48px)}100%{opacity:0;transform:translate(-120px,48px)}}.ss-meteors-d{animation-duration:12s}`);
  o+=`<defs><linearGradient id="ss-meteors-tl" x1="0" x2="1"><stop offset="0" stop-color="var(--snow)" stop-opacity=".95"/><stop offset="1" stop-color="var(--snow)" stop-opacity="0"/></linearGradient><linearGradient id="ss-meteors-td" x1="0" x2="1"><stop offset="0" stop-color="var(--m3s)" stop-opacity=".95"/><stop offset="1" stop-color="var(--orb)" stop-opacity="0"/></linearGradient></defs>`;
  const m=(x,y,d,day,k)=>`<g transform="translate(${x} ${y})"><g class="ss-meteors-m${day?' ss-meteors-d':''}" style="animation-delay:${d}s"><g transform="rotate(-22)"><rect x="0" y="-.8" width="${day?64:48}" height="${day?1.8:1.7}" rx=".8" fill="url(#ss-meteors-t${day?'d':'l'})"/>${day?'<circle cx="0" cy="0" r="3.6" fill="var(--glow)" opacity=".8"/><circle cx="0" cy="0" r="1.6" fill="var(--m3s)"/>':'<circle cx="0" cy="0" r="1.7" fill="var(--snow)"/>'}</g></g></g>`;
  let n='';[[520,8,-1],[430,4,-3.7],[560,26,-6.3],[360,10,-8.1],[300,22,-4.8]].forEach(([x,y,d],k)=>n+=m(x,y,d,false,k));
  let dy='';[[520,10,-1],[430,4,-5],[360,16,-9]].forEach(([x,y,d],k)=>dy+=m(x,y,d,true,k));
  return o+`<g class="sc-night">${n}</g><g class="sc-day">${dy}</g>`;
}});

/* 5. Autumn: tumbling leaves */
SEASONS.push({id:'autumn',name:'Autumn',group:'Seasons and nature',when:'Around September 22',moves:'Warm leaves tumble down the band',draw(){
  const cols=['var(--z1)','var(--z2)','var(--lite)','var(--bad)'];
  const shape=i=>`<path d="M0 -4.2Q3.6 -1.4 0 4Q-3.6 -1.4 0 -4.2Z" fill="${cols[i%4]}" opacity="${i%4===3?.8:.95}"/><path d="M0 -3L0 5" stroke="var(--bad)" stroke-width=".5" opacity=".5"/>`;
  return sty(fallCss('autumn',true))+fall('autumn',15,{s:7,x0:200,x1:600,y0:-6,yr:18,dx0:50,dxr:70,dy0:70,dyr:30,rot:330,d0:12,dr:7,shape});
}});

/* 6. Harvest: wheat and gourds */
SEASONS.push({id:'harvest',name:'Harvest',group:'Seasons and nature',when:'Thanksgiving week',moves:'Wheat sways at the corner beside two small gourds',draw(){
  let o=sty(`.ss-harvest-wh{animation:ss-harvest-sw 4.5s ease-in-out infinite alternate;transform-box:fill-box;transform-origin:50% 100%}@keyframes ss-harvest-sw{from{transform:rotate(-5deg)}to{transform:rotate(6deg)}}`);
  [[530,30,0],[540,38,-1.2],[549,32,-2.4],[558,40,-.6],[294,24,-.8],[303,31,-2],[312,26,-3.1]].forEach(([x,h,d])=>{
    const hd=[0,1,2,3].map(k=>`<ellipse cx="-2.1" cy="${-h+k*4.6}" rx="1.5" ry="2.6" transform="rotate(-24 -2.1 ${-h+k*4.6})" fill="var(--z2)"/><ellipse cx="2.1" cy="${-h+k*4.6+1.2}" rx="1.5" ry="2.6" transform="rotate(24 2.1 ${-h+k*4.6+1.2})" fill="var(--lite)"/>`).join('');
    o+=`<g transform="translate(${x} 112)"><g class="ss-harvest-wh" style="animation-delay:${d}s"><path d="M0 0L0 ${-h}" stroke="var(--z2)" stroke-width="1.1" opacity=".85"/>${hd}<ellipse cx="0" cy="${-h-3.4}" rx="1.3" ry="3" fill="var(--z2)"/></g></g>`;});
  const gourd=(x,y,s,c)=>`<g transform="translate(${x} ${y}) scale(${s})"><ellipse cx="-3.4" cy="0" rx="4.4" ry="5.2" fill="${c}"/><ellipse cx="3.4" cy="0" rx="4.4" ry="5.2" fill="${c}"/><ellipse cx="0" cy="0" rx="3.4" ry="5.6" fill="${c}"/><path d="M0 -5.4L0 5.4M-3.6 -4.2Q-4.8 0 -3.6 4.2M3.6 -4.2Q4.8 0 3.6 4.2" stroke="var(--bad)" stroke-width=".6" fill="none" opacity=".45"/><path d="M0 -5.4Q.8 -8.4 2.6 -9" stroke="var(--z3)" stroke-width="1.4" fill="none" stroke-linecap="round"/></g>`;
  o+=gourd(500,104,.9,'var(--z1)')+gourd(514,106,.65,'var(--z2)');
  return o;
}});

/* 7. Longest night: extra stars and gentle snow */
SEASONS.push({id:'longnight',name:'Longest night',group:'Seasons and nature',when:'Around December 21',moves:'Extra stars twinkle and soft snow falls',draw(){
  let o=sty(fallCss('longnight',false)+`.ss-longnight-st{animation:ss-longnight-tw 4s ease-in-out infinite}@keyframes ss-longnight-tw{0%,100%{opacity:.15;transform:scale(.7)}50%{opacity:1;transform:scale(1.25)}}.ss-longnight-st{transform-box:fill-box;transform-origin:center}`);
  const flake=c=>i=>`<circle r="${f1(1.1+rnd(i)*.9)}" fill="${c}"/>`;
  let stars='';[[214,10],[240,34],[262,16],[286,44],[308,8],[330,28],[352,14],[488,12],[510,30],[530,8],[546,40],[240,56]].forEach(([x,y],i)=>{
    stars+=`<g transform="translate(${x} ${y})"><g class="ss-longnight-st" style="animation-duration:${f1(3+rnd(i+5)*2.5)}s;animation-delay:${f1(-rnd(i+9)*5)}s"><path d="M0 -2.6L.6 -.6L2.6 0L.6 .6L0 2.6L-.6 .6L-2.6 0L-.6 -.6Z" fill="var(--snow)"/></g></g>`;});
  const snowN=fall('longnight',12,{s:3,x0:200,x1:600,y0:-6,yr:14,dx0:6,dxr:16,dy0:78,dyr:26,rot:0,d0:11,dr:6,shape:flake('var(--snow)')});
  const snowD=fall('longnight',12,{s:3,x0:200,x1:600,y0:-6,yr:14,dx0:6,dxr:16,dy0:78,dyr:26,rot:0,d0:11,dr:6,shape:flake('var(--m3)')});
  return o+`<g class="sc-night">${stars}${snowN}</g><g class="sc-day">${snowD}</g>`;
}});
})();
