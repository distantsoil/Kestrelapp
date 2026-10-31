/* Kestrel scenes, set A: desert, prairie, forest, city, lake, farmland. Registers into window.SCENES. */
window.SCENES=window.SCENES||[];
(function(){
const W=560,H=110;
const f1=n=>Math.round(n*10)/10;
const rnd=i=>{const x=Math.sin(i*127.1+311.7)*43758.5453;return x-Math.floor(x);};
const anim=(css)=>`<style>@media (prefers-reduced-motion:no-preference){${css}}</style>`;
/* Shared frame: left fade mask, sun/moon with halo. body is drawn inside the mask. */
function frame(id,ox,oy,body,css,r){
  r=r||11;
  return anim(css)+`<defs><linearGradient id="sc-${id}-fd" x1="0" x2="1"><stop offset=".17" stop-color="#fff" stop-opacity="0"/><stop offset=".44" stop-color="#fff"/></linearGradient><mask id="sc-${id}-mk"><rect width="${W}" height="${H}" fill="url(#sc-${id}-fd)"/></mask><radialGradient id="sc-${id}-gl"><stop offset="0" stop-color="var(--glow)" stop-opacity=".9"/><stop offset="1" stop-color="var(--glow)" stop-opacity="0"/></radialGradient><linearGradient id="sc-${id}-g1" x1="0" y1="0" x2="0" y2="1"><stop offset="0" stop-color="var(--m1)"/><stop offset="1" stop-color="var(--m1)" stop-opacity=".5"/></linearGradient></defs><g mask="url(#sc-${id}-mk)"><circle class="sc-${id}-halo" cx="${ox}" cy="${oy}" r="30" fill="url(#sc-${id}-gl)"/><circle cx="${ox}" cy="${oy}" r="${r}" fill="var(--orb)"/>${body}</g>`;
}
const haloCss=id=>`.sc-${id}-halo{animation:sc-${id}-br 7s ease-in-out infinite;transform-origin:center;transform-box:fill-box}@keyframes sc-${id}-br{50%{opacity:.4;transform:scale(1.16)}}`;
const samp=(fn,x0,x1,st)=>{const a=[];for(let x=x0;x<=x1;x+=st)a.push([x,fn(x)]);return a;};
const poly=(a,bot)=>'M'+a.map(p=>f1(p[0])+' '+f1(p[1])).join('L')+`L${a[a.length-1][0]} ${bot}L${a[0][0]} ${bot}Z`;
/* Pine silhouette: three tiers, optional shaded right half, optional sway class. */
function pine(x,b,h,f,sh,cls,dl){
  const w=h*.34,R=[[.6,.62],[.28,.62],[.85,.3],[.4,.3],[1.1,.05]];
  const rp=R.map(p=>[x+w*p[0],b-h*p[1]]),lp=R.map(p=>[x-w*p[0],b-h*p[1]]).reverse();
  const P=a=>a.map(p=>f1(p[0])+' '+f1(p[1])).join('L');
  const body=`M${f1(x)} ${f1(b-h)}L${P(rp)}L${f1(x+w*.14)} ${f1(b-h*.05)}L${f1(x+w*.14)} ${f1(b)}L${f1(x-w*.14)} ${f1(b)}L${f1(x-w*.14)} ${f1(b-h*.05)}L${P(lp)}Z`;
  const shade=sh?`<path d="M${f1(x)} ${f1(b-h)}L${P(rp)}L${f1(x)} ${f1(b-h*.05)}Z" fill="${sh}" opacity=".55"/>`:'';
  const g=`<path d="${body}" fill="${f}"/>${shade}`;
  return cls?`<g class="${cls}" style="animation-delay:${dl||0}s">${g}</g>`:g;
}
/* ---------- DESERT ---------- */
function mesa(x,y,wt,wb,f,s){
  const hw=wt/2,bw=wb/2,ch=(H-y)*.3,cp=y+5,ty=y+ch+(H-y-ch)*.38;
  const body=`M${x-bw} ${H}C${x-bw+wb*.12} ${f1(ty+8)} ${f1(x-hw-4)} ${f1(ty)} ${f1(x-hw)} ${f1(y+ch)}L${f1(x-hw)} ${cp}L${f1(x-hw-3)} ${cp}L${f1(x-hw-3)} ${y}L${f1(x+hw+3)} ${y}L${f1(x+hw+3)} ${cp}L${f1(x+hw)} ${cp}L${f1(x+hw)} ${f1(y+ch)}C${f1(x+hw+4)} ${f1(ty)} ${f1(x+bw-wb*.12)} ${f1(ty+8)} ${x+bw} ${H}Z`;
  const sh=`M${f1(x+wt*.18)} ${cp}L${f1(x+hw)} ${cp}L${f1(x+hw)} ${f1(y+ch)}C${f1(x+hw+4)} ${f1(ty)} ${f1(x+bw-wb*.12)} ${f1(ty+8)} ${x+bw} ${H}L${f1(x+wb*.2)} ${H}C${f1(x+wb*.18)} ${f1(ty+10)} ${f1(x+wt*.2)} ${f1(ty)} ${f1(x+wt*.18)} ${f1(y+ch)}Z`;
  return `<path d="${body}" fill="${f}"/><path d="${sh}" fill="${s}"/><path d="M${f1(x+wt*.18)} ${y}L${f1(x+hw+3)} ${y}L${f1(x+hw+3)} ${cp}L${f1(x+wt*.18)} ${cp}Z" fill="${s}" opacity=".5"/>`;
}
function saguaro(x,b,h,f,s,cls,dl){
  const t=h*.15,r=t/2;let o=`<rect x="${f1(x-r)}" y="${f1(b-h)}" width="${f1(t)}" height="${f1(h)}" rx="${f1(r)}" fill="${f}"/>`;
  o+=`<rect x="${f1(x-r*4.4)}" y="${f1(b-h*.5)}" width="${f1(r*5)}" height="${f1(t)}" rx="${f1(r)}" fill="${f}"/><rect x="${f1(x-r*4.4)}" y="${f1(b-h*.78)}" width="${f1(t)}" height="${f1(h*.36)}" rx="${f1(r)}" fill="${f}"/>`;
  o+=`<rect x="${f1(x-r*.6)}" y="${f1(b-h*.62)}" width="${f1(r*5)}" height="${f1(t)}" rx="${f1(r)}" fill="${f}"/><rect x="${f1(x+r*3.4)}" y="${f1(b-h*.86)}" width="${f1(t)}" height="${f1(h*.38)}" rx="${f1(r)}" fill="${f}"/>`;
  o+=`<rect x="${f1(x+r*.1)}" y="${f1(b-h)}" width="${f1(r*.9)}" height="${f1(h)}" rx="${f1(r*.45)}" fill="${s}" opacity=".5"/>`;
  return `<g class="${cls}" style="animation-delay:${dl}s">${o}</g>`;
}
function sceneDesert(){
  let b='<g class="sc-desert-far">';
  [[300,50,96,150],[420,64,22,50],[540,46,84,130]].forEach(m=>b+=mesa(m[0],m[1],m[2],m[3],'url(#sc-desert-g1)','var(--m1s)'));
  b+='</g>';
  [[246,80,50,90],[368,72,66,110],[500,76,70,116]].forEach(m=>b+=mesa(m[0],m[1],m[2],m[3],'var(--m2)','var(--m2s)'));
  b+=`<ellipse class="sc-desert-hz1" cx="390" cy="86" rx="230" ry="14" fill="url(#sc-desert-hg)"/><ellipse class="sc-desert-hz2" cx="400" cy="70" rx="230" ry="11" fill="url(#sc-desert-hg)"/>`;
  b+=`<path d="M120 ${H}C200 96 320 100 380 94S500 88 ${W} 92L${W} ${H}Z" fill="var(--m3)"/><path d="M300 ${H}C350 100 410 100 450 97S530 98 ${W} 100L${W} ${H}Z" fill="var(--m3s)" opacity=".55"/>`;
  b+=saguaro(338,104,26,'var(--tree)','var(--tree3)','sc-desert-sw',0)+saguaro(512,106,34,'var(--tree2)','var(--tree3)','sc-desert-sw',-3)+saguaro(432,100,17,'var(--tree)','var(--tree3)','sc-desert-sw',-5);
  [[372,104,5],[470,106,4],[544,108,5],[290,106,4]].forEach(([x,y,r])=>{b+=`<path d="M${x-r*1.4} ${y}Q${x-r} ${y-r*1.3} ${x} ${y-r*1.3}Q${x+r} ${y-r*1.3} ${x+r*1.4} ${y}Z" fill="var(--tree)" opacity=".75"/>`;});
  b+=`<g transform="translate(0 105)"><g class="sc-desert-dd"><g class="sc-desert-sp" fill="var(--sand2)" opacity=".75"><ellipse cx="0" cy="-2" rx="7" ry="1.7"/><ellipse cx="1" cy="-7" rx="5.5" ry="1.6"/><ellipse cx="-1" cy="-12" rx="4.2" ry="1.5"/><ellipse cx="1" cy="-17" rx="3" ry="1.3"/><ellipse cx="0" cy="-21" rx="2" ry="1"/></g></g></g>`;
  b+=kestrel(350,30,.8);
  const css=haloCss('desert')+`.sc-desert-hz1{animation:sc-desert-ha 5s ease-in-out infinite alternate}.sc-desert-hz2{animation:sc-desert-ha 6.5s ease-in-out -2.5s infinite alternate}@keyframes sc-desert-ha{from{opacity:.1;transform:translateY(4px)}to{opacity:.5;transform:translateY(-7px)}}.sc-desert-dd{animation:sc-desert-dv 22s ease-in-out infinite alternate}@keyframes sc-desert-dv{from{transform:translateX(540px)}to{transform:translateX(250px)}}.sc-desert-sp{animation:sc-desert-sk 3s ease-in-out infinite alternate;transform-box:fill-box;transform-origin:50% 100%}@keyframes sc-desert-sk{from{transform:skewX(-9deg)}to{transform:skewX(9deg)}}`+`.sc-desert-far{animation:sc-desert-hz 3.5s ease-in-out infinite alternate;transform-box:fill-box;transform-origin:50% 100%}@keyframes sc-desert-hz{from{transform:translateX(-2px) scale(1,1)}to{transform:translateX(2px) scale(1,1.02)}}.sc-desert-sw{animation:sc-desert-sy 9s ease-in-out infinite alternate;transform-origin:bottom;transform-box:fill-box}@keyframes sc-desert-sy{to{transform:skewX(1.8deg)}}`;
  return frame('desert',420,34,b,css).replace('</defs>','<radialGradient id="sc-desert-hg"><stop offset="0" stop-color="var(--mist)" stop-opacity=".85"/><stop offset=".6" stop-color="var(--mist)" stop-opacity=".4"/><stop offset="1" stop-color="var(--mist)" stop-opacity="0"/></radialGradient></defs>');
}
/* ---------- PRAIRIE ---------- */
function windmill(x,b,hy,r,c1,c2,dl,cls){
  const tw=r*.5,bw=r*.55;let o=`<path d="M${f1(x-bw)} ${b}L${f1(x-tw*.4)} ${hy+r*.1}L${f1(x+tw*.4)} ${hy+r*.1}L${f1(x+bw)} ${b}Z" fill="${c1}"/>`;
  [.35,.6,.82].forEach(t=>{const y=hy+(b-hy)*t,wd=tw*.4+(bw-tw*.4)*t;o+=`<path d="M${f1(x-wd)} ${f1(y)}L${f1(x+wd)} ${f1(y)}" stroke="${c2}" stroke-width=".9" opacity=".8"/>`;});
  o+=`<path d="M${f1(x-bw*.55)} ${f1(hy+(b-hy)*.35)}L${f1(x+bw*.8)} ${f1(hy+(b-hy)*.6)}M${f1(x+bw*.55)} ${f1(hy+(b-hy)*.35)}L${f1(x-bw*.8)} ${f1(hy+(b-hy)*.6)}" stroke="${c2}" stroke-width=".8" opacity=".6"/>`;
  o+=`<path d="M${x} ${hy}L${f1(x+r*1.3)} ${hy}" stroke="${c1}" stroke-width="${f1(Math.max(.8,r*.07))}"/><path d="M${f1(x+r*1.15)} ${f1(hy-r*.02)}L${f1(x+r*1.65)} ${f1(hy-r*.3)}L${f1(x+r*1.65)} ${f1(hy+r*.3)}L${f1(x+r*1.15)} ${f1(hy+r*.1)}Z" fill="${c1}"/>`;
  let bl='';for(let i=0;i<12;i++){bl+=`<path transform="rotate(${i*30})" d="M0 ${f1(-r*.14)}L${f1(r*.07)} ${f1(-r)}L${f1(-r*.07)} ${f1(-r)}Z" fill="${c2}"/>`;}
  o+=`<g transform="translate(${x} ${hy})"><g class="${cls}" style="animation-delay:${dl}s">${bl}<circle r="${f1(r)}" fill="none" stroke="${c2}" stroke-width=".8" opacity=".8"/></g><circle r="${f1(Math.max(1.2,r*.13))}" fill="${c1}"/></g>`;
  return o;
}
function scenePrairie(){
  let b=`<path d="M120 76C250 68 300 70 350 67S450 64 ${W} 67L${W} ${H}Z" fill="url(#sc-prairie-g1)"/>`;
  b+=`<path d="M120 82C260 78 330 79 400 77S510 76 ${W} 77L${W} ${H}Z" fill="var(--m2)"/>`;
  for(let i=-3;i<=9;i++){const xx=400+i*52;b+=`<path d="M404 78L${xx} ${H}L${xx+16} ${H}L406 78Z" fill="var(--m2s)" opacity=".22"/>`;}
  b+=`<path d="M120 ${H}C220 98 330 100 400 96S520 92 ${W} 94L${W} ${H}Z" fill="var(--m3)"/>`;
  /* fence in perspective toward the horizon */
  let fp='',fr='';
  for(let k=0;k<9;k++){const t=k/8,x=210+t*210,y=103-t*13,h=11-t*6,w=1.8-t*.8;fp+=`<rect x="${f1(x)}" y="${f1(y-h)}" width="${f1(w)}" height="${f1(h)}" fill="var(--m3s)"/>`;}
  fr=`<path d="M210 94.5L420 86.5M210 98.5L420 88.5" stroke="var(--m3s)" stroke-width="1" fill="none" opacity=".8"/>`;
  b+=fr+fp;
  [[400,86,0],[330,93,-3],[470,99,-6]].forEach(([x,y,d])=>b+=`<ellipse class="sc-prairie-gu" style="animation-delay:${d}s" cx="${x}" cy="${y}" rx="52" ry="2.6" fill="var(--snow)"/>`);
  b+=`<g class="sc-prairie-cl"><ellipse cx="300" cy="24" rx="22" ry="5" fill="var(--snow)" opacity=".6"/><ellipse cx="312" cy="20" rx="13" ry="5" fill="var(--snow)" opacity=".6"/><ellipse cx="289" cy="21" rx="11" ry="4" fill="var(--snow)" opacity=".6"/></g>`;
  const wm=(c1,c2)=>windmill(468,99,52,17,c1,c2,0,'sc-prairie-rot');
  b+=`<g class="sc-day">${wm('var(--tree)','var(--tree2)')}</g><g class="sc-night">${wm('var(--snow)','var(--snow)')}</g>`;
  [[236,105,9],[262,107,11],[300,106,8],[344,107,10],[510,107,9],[540,106,11]].forEach(([x,y,h],i)=>{const c=i%2?'var(--tree2)':'var(--tree)';
    b+=`<path class="sc-prairie-sw" style="animation-delay:${f1(-i*1.1)}s" d="M${x-3} ${y}Q${x-3.5} ${y-h*.6} ${x-4.5} ${y-h*.9}Q${x-1.5} ${y-h*.6} ${x-1} ${y}ZM${x-1.5} ${y}Q${x-1} ${y-h*.8} ${x} ${y-h}Q${x+1} ${y-h*.7} ${x+1.5} ${y}ZM${x+1} ${y}Q${x+2.5} ${y-h*.6} ${x+4.5} ${y-h*.8}Q${x+3.5} ${y-h*.4} ${x+3} ${y}Z" fill="${c}"/>`;});
  b+=kestrel(310,34,.8);
  const css=haloCss('prairie')+`.sc-prairie-rot{animation:sc-prairie-sp 13s linear infinite;transform-box:fill-box;transform-origin:center}@keyframes sc-prairie-sp{to{transform:rotate(360deg)}}.sc-prairie-gu{animation:sc-prairie-gs 9s ease-in-out infinite;opacity:0}@keyframes sc-prairie-gs{0%{transform:translateX(80px);opacity:0}50%{opacity:.5}100%{transform:translateX(-80px);opacity:0}}.sc-prairie-cl{animation:sc-prairie-cd 30s ease-in-out infinite alternate}@keyframes sc-prairie-cd{from{transform:translateX(110px)}to{transform:translateX(-60px)}}.sc-prairie-sw{animation:sc-prairie-sy 5s ease-in-out infinite alternate;transform-origin:bottom;transform-box:fill-box}@keyframes sc-prairie-sy{from{transform:skewX(-6deg)}to{transform:skewX(8deg)}}`;
  return frame('prairie',386,36,b,css);
}
/* ---------- FOREST ---------- */
function sceneForest(){
  let b='';
  const layer=(fn,x0,x1,st,hmin,hmax,f,sh,seed,swayCls,tr)=>{
    let o=`<path d="${poly(samp(fn,0,x1,4),H)}" fill="${f}"/>`;
    for(let x=x0+2,i=0;x<x1;x+=st*(.8+rnd(i+seed)*.5),i++){
      const h=hmin+rnd(i*3+seed)*(hmax-hmin),c=tr&&i%2?tr:f;o+=pine(x,fn(x)+3,h,c,sh,swayCls,-i*.6);
    }
    return o;
  };
  const far=x=>58-5*Math.sin(x/41)-3*Math.sin(x/17+1),mid=x=>72-4*Math.sin(x/33+2)-2.5*Math.sin(x/13),near=x=>90-4*Math.sin(x/29+1)-2*Math.sin(x/11);
  b+=layer(far,120,562,8,12,19,'var(--m1)','var(--m1s)',1);
  b+=`<ellipse class="sc-forest-ms1" cx="390" cy="64" rx="230" ry="15" fill="url(#sc-forest-mg)"/>`;
  b+=layer(mid,130,562,9,17,27,'var(--m2)','var(--m2s)',9);
  b+=`<ellipse class="sc-forest-ms2" cx="400" cy="82" rx="230" ry="15" fill="url(#sc-forest-mg)"/>`;
  b+=layer(near,140,562,12,22,36,'var(--m3)',null,17,'sc-forest-sw');
  /* near pines drawn in tree tones on top */
  let nt='';for(let x=146,i=0;x<562;x+=15+rnd(i+5)*9,i++){const h=24+rnd(i+40)*16;nt+=pine(x,near(x)+5,h,i%2?'var(--tree2)':'var(--tree)','var(--tree3)','sc-forest-sw',-i*.8);}
  b+=nt;
  b+=`<ellipse class="sc-forest-ms3" cx="420" cy="96" rx="200" ry="12" fill="url(#sc-forest-mg)"/>`;
  for(let i=0;i<14;i++){const x=268+rnd(i+70)*280,y=66+rnd(i+90)*34,d=f1(7+rnd(i+110)*5),dl=f1(-rnd(i+130)*10);
    b+=`<circle class="sc-night sc-forest-ff" cx="${f1(x)}" cy="${f1(y)}" r="1.4" fill="var(--lite)" style="animation-duration:${d}s;animation-delay:${dl}s"/>`;}
  b+=kestrel(316,26,.8);
  const css=haloCss('forest')+`.sc-forest-ms1{animation:sc-forest-d 16s ease-in-out infinite alternate}.sc-forest-ms2{animation:sc-forest-d 20s ease-in-out -8s infinite alternate-reverse}.sc-forest-ms3{animation:sc-forest-d 14s ease-in-out -4s infinite alternate}@keyframes sc-forest-d{from{transform:translateX(30px);opacity:.55}to{transform:translateX(-30px);opacity:1}}.sc-forest-sw{animation:sc-forest-sy 7s ease-in-out infinite alternate;transform-origin:bottom;transform-box:fill-box}@keyframes sc-forest-sy{to{transform:skewX(3.4deg)}}.sc-forest-ff{animation:sc-forest-fl 9s ease-in-out infinite}@keyframes sc-forest-fl{0%,100%{opacity:0;transform:translate(0,0)}30%{opacity:.9}60%{opacity:.8;transform:translate(9px,-8px)}85%{opacity:0;transform:translate(-5px,-13px)}}`;
  const out=frame('forest',432,32,b,css);
  return out.replace('</defs>',`<radialGradient id="sc-forest-mg"><stop offset="0" stop-color="var(--mist)" stop-opacity=".8"/><stop offset=".55" stop-color="var(--mist)" stop-opacity=".4"/><stop offset="1" stop-color="var(--mist)" stop-opacity="0"/></radialGradient></defs>`);
}
/* ---------- CITY ---------- */
function sceneCity(){
  let b='',tw='',tg='';
  const bld=(x,w,t,f,s)=>`<rect x="${x}" y="${t}" width="${w}" height="${H-t}" fill="${f}"/><rect x="${f1(x+w*.62)}" y="${t}" width="${f1(w*.38)}" height="${H-t}" fill="${s}"/>`;
  const cand=[];
  const wins=(x,w,t,bot,seed)=>{for(let yy=t+5;yy<bot;yy+=7){for(let xx=x+3;xx<x+w-3;xx+=6){const r=rnd(seed+xx*1.7+yy*3.1);
    if(r<.34)tw+=`<rect x="${xx}" y="${yy}" width="2.6" height="3.2" fill="var(--lite)" opacity=".85"/>`;else if(r<.46&&xx>215)cand.push([xx,yy]);}}};
  [[120,24,98],[146,22,90],[172,26,82],[205,24,70],[232,20,58],[256,28,62],[288,22,48],[314,26,56],[344,20,42],[368,30,52],[402,22,46],[428,26,60],[458,20,50],[482,30,40],[516,24,56],[544,30,46]].forEach(a=>b+=bld(a[0],a[1],a[2],'url(#sc-city-g1)','var(--m1s)'));
  const mid=[[128,22,100],[154,20,96],[182,24,90],[214,20,84],[238,24,72],[266,18,78],[288,26,62],[318,20,68],[342,28,50],[374,22,64],[400,26,58],[430,20,72],[452,26,46],[482,22,66],[508,28,52],[540,26,62]];
  mid.forEach(a=>b+=bld(a[0],a[1],a[2],'var(--m2)','var(--m2s)'));
  b+=`<path d="M465 46L465 30" stroke="var(--m2s)" stroke-width="1.2"/><circle cx="465" cy="29.5" r="1.1" fill="var(--bad)"/>`;
  /* water tower on a mid rooftop */
  b+=`<path d="M404 58L407 49M416 58L413 49M407 49L413 49M410 58L410 49" stroke="var(--m3s)" stroke-width="1"/><rect x="403.5" y="40" width="13" height="9.5" rx="1" fill="var(--m3)"/><path d="M402.5 40L410 35L417.5 40Z" fill="var(--m3s)"/><rect x="412" y="40" width="4.5" height="9.5" fill="var(--m3s)" opacity=".6"/>`;
  mid.forEach((a,i)=>wins(a[0],a[1],a[2],H-24,i*13.7,i%3===0));
  b+=`<rect x="0" y="98" width="${W}" height="12" fill="var(--m3)"/>`;
  const near=[[104,30,104],[138,24,102],[166,28,100],[198,30,94],[232,22,88],[258,26,96],[288,20,90],[312,30,98],[346,24,92],[374,32,100],[410,22,94],[436,28,90],[468,24,98],[496,30,92],[530,30,96]];
  near.forEach(a=>b+=bld(a[0],a[1],a[2],'var(--m3)','var(--m3s)'));
  near.forEach((a,i)=>wins(a[0],a[1],a[2],H-4,i*7.3+50,i%4===1));
  const step=Math.floor(cand.length/10);
  for(let k=0;k<10;k++){const c=cand[k*step];tg+=`<rect class="sc-city-tg" x="${c[0]}" y="${c[1]}" width="2.6" height="3.2" fill="var(--lite)" style="animation-duration:${f1(4.5+rnd(k+3)*2.5)}s;animation-delay:${f1(-rnd(k+9)*7)}s"/>`;}
  let gl='';for(let k=0;k<8;k++){const c=cand[k*step+1];gl+=`<rect class="sc-city-gl" x="${c[0]-.3}" y="${c[1]-.3}" width="3.2" height="3.8" fill="var(--snow)" style="animation-duration:${f1(5+rnd(k+21)*3)}s;animation-delay:${f1(-rnd(k+31)*8)}s"/>`;}
  b+=`<g class="sc-night">${tw}</g><g class="sc-night">${tg}</g><g class="sc-day">${gl}</g>`;
  b+=`<g class="sc-city-pl"><g transform="scale(-1.35 1.35)"><path class="sc-city-ct" d="M-95 0L-10 0" stroke="var(--snow)" stroke-width="1.3" opacity=".6"/><path d="M-8 0q0 -1.8 3 -1.8L7 -1.8q2.5 0 2.5 1.8q0 1.6 -2.5 1.6L-5 1.6q-3 0 -3 -1.6Z" fill="var(--bird)"/><path d="M-7 -1.6L-9 -5L-6.2 -5L-3.5 -1.6Z" fill="var(--bird)"/><path d="M-1 0L-4 4.5L-1.5 4.5L3 0Z" fill="var(--bird)" opacity=".85"/></g></g>`;
  b+=kestrel(272,38,.8);
  const css=haloCss('city')+`.sc-city-pl{animation:sc-city-fl 28s linear infinite}@keyframes sc-city-fl{from{transform:translate(640px,13px)}to{transform:translate(110px,22px)}}.sc-city-ct{transform-box:fill-box;transform-origin:100% 50%;animation:sc-city-cg 28s linear infinite}@keyframes sc-city-cg{0%{transform:scaleX(.1)}50%,100%{transform:scaleX(1)}}.sc-city-tg{animation:sc-city-tw 6s ease-in-out infinite;opacity:0}@keyframes sc-city-tw{0%,6%{opacity:0}17%,52%{opacity:.95}63%,100%{opacity:0}}.sc-city-gl{animation:sc-city-gg 7s ease-in-out infinite;opacity:0}@keyframes sc-city-gg{0%,100%{opacity:0}50%{opacity:.85}}`;
  return frame('city',448,32,b,css);
}
/* ---------- LAKE ---------- */
function sceneLake(){
  let hills=`<path d="M100 70C140 66 160 62 190 70C230 54 270 46 310 52C350 58 380 38 430 40C480 42 520 50 ${W} 42L${W} 70Z" fill="url(#sc-lake-g1)"/><path d="M360 70C390 56 410 44 430 40C450 40 470 42 480 44C470 56 440 64 420 70Z" fill="var(--m1s)" opacity=".55"/><path d="M236 70C290 62 330 64 370 60C420 56 450 64 500 58C530 55 545 57 ${W} 56L${W} 70Z" fill="var(--m2)"/><path d="M300 70C340 66 380 66 420 64L440 70Z" fill="var(--m2s)" opacity=".6"/>`;
  [[452,64,13],[462,62,17],[472,63,12],[484,61,19],[496,62,14],[508,60,18],[522,61,13],[536,59,17],[550,60,14]].forEach(([x,y,h],i)=>hills+=pine(x,y+3,h,i%2?'var(--tree2)':'var(--tree)','var(--tree3)'));
  let b=`<defs><linearGradient id="sc-lake-wg" x1="0" y1="0" x2="0" y2="1"><stop offset="0" stop-color="var(--water)"/><stop offset="1" stop-color="var(--water2)"/></linearGradient><clipPath id="sc-lake-cp"><rect x="0" y="70" width="610" height="40"/></clipPath></defs>`;
  b+=`<g id="sc-lake-hills">${hills}</g>`;
  b+=`<rect x="0" y="70" width="610" height="40" fill="url(#sc-lake-wg)"/>`;
  b+=`<g clip-path="url(#sc-lake-cp)"><use href="#sc-lake-hills" transform="translate(0 140) scale(1 -1)" opacity=".38"/></g>`;
  b+=`<rect x="0" y="70" width="610" height="1.2" fill="var(--snow)" opacity=".45"/>`;
  [[0,14,76],[1,10,82],[2,7,88],[3,4,94]].forEach(([k,w,y])=>b+=`<rect class="sc-lake-gl" style="animation-delay:${-k*1.3}s" x="${420-w/2}" y="${y}" width="${w}" height="1.6" rx=".8" fill="var(--orb)" opacity=".55"/>`);
  for(let k=0;k<3;k++)b+=`<path class="sc-lake-rp" style="animation-delay:${-k*2.2}s" d="M170 ${80+k*9} q12 -3 24 0 t24 0 t24 0 t24 0 t24 0 t24 0 t24 0 t24 0 t24 0 t24 0 t24 0 t24 0 t24 0 t24 0 t24 0 t24 0" fill="none" stroke="var(--snow)" stroke-width="1.2" opacity="${f1(.5-k*.1)}"/>`;
  b+=`<g class="sc-lake-bt"><g transform="translate(386 74)"><path d="M-7 4.5L7 4.5L5 7.5L-5 7.5Z" fill="var(--m3s)" opacity=".28"/><path d="M0 -17L0 -1L-8 -1Z" fill="var(--snow)"/><path d="M1.6 -13L1.6 -1L8.5 -1Z" fill="var(--sand)"/><path d="M-8.5 0L8.5 0L6 3.6L-6 3.6Z" fill="var(--tree)"/><path d="M0 -17L0 0" stroke="var(--tree)" stroke-width=".8"/></g></g>`;
  b+=`<path d="M470 ${H}C490 98 520 92 ${W} 90L${W} ${H}Z" fill="var(--m3)"/>`;
  [[506,99,22],[522,97,30],[538,96,24],[552,95,32]].forEach(([x,y,h],i)=>b+=pine(x,y,h,i%2?'var(--tree2)':'var(--tree)','var(--tree3)','sc-lake-sw',-i*.8));
  b+=kestrel(304,30,.8);
  const css=haloCss('lake')+`.sc-lake-rp{animation:sc-lake-w 7s ease-in-out infinite alternate}@keyframes sc-lake-w{to{transform:translateX(-38px)}}.sc-lake-gl{animation:sc-lake-g 6s ease-in-out infinite alternate;transform-box:fill-box;transform-origin:center}@keyframes sc-lake-g{from{opacity:.2;transform:scaleX(.6)}to{opacity:.8;transform:scaleX(1.3)}}.sc-lake-bt{animation:sc-lake-sl 26s ease-in-out infinite alternate}@keyframes sc-lake-sl{from{transform:translate(34px,0) rotate(-2deg)}to{transform:translate(-50px,1px) rotate(2.4deg)}}.sc-lake-sw{animation:sc-lake-sy 8s ease-in-out infinite alternate;transform-origin:bottom;transform-box:fill-box}@keyframes sc-lake-sy{to{transform:skewX(3deg)}}`;
  return frame('lake',420,34,b,css);
}
/* ---------- FARMLAND ---------- */
function sceneFarm(){
  let b='';
  const fields=(id,hill,yt,cols,seed)=>{
    const vx=400,vy=8;let o=`<clipPath id="sc-farm-${id}"><path d="${hill}"/></clipPath><g clip-path="url(#sc-farm-${id})">`;
    let x=-20,i=0;while(x<600){const w=16+rnd(i*5+seed)*14,k=(110-vy)/(yt-vy),xb=a=>vx+(a-vx)*k;
      o+=`<path d="M${f1(x)} ${yt}L${f1(x+w)} ${yt}L${f1(xb(x+w))} 110L${f1(xb(x))} 110Z" fill="${cols[i%cols.length]}"/>`;x+=w;i++;}
    return o+`</g>`;};
  const far='M0 82C100 78 170 76 190 70C240 56 290 58 340 52S450 48 500 55S545 52 560 52L560 110L0 110Z';
  const mid='M0 100C100 94 170 92 196 86C260 72 320 76 380 70S500 66 560 72L560 110L0 110Z';
  const nr='M0 110L196 110C250 98 330 96 400 92S520 90 560 92L560 110Z';
  b+=`<path d="${far}" fill="url(#sc-farm-g1)"/>`+fields('f',far,48,['var(--m1)','var(--sand)','var(--m1s)'],3);
  b+=`<path d="${mid}" fill="var(--m2)"/>`+fields('m',mid,66,['var(--m2)','var(--leaf)','var(--m2s)'],8);
  b+=`<g transform="translate(508 82) scale(.7) translate(-508 -82)"><rect x="518" y="62" width="8" height="21" fill="var(--sand)"/><rect x="522.5" y="62" width="3.5" height="21" fill="var(--sand2)"/><path d="M518 62Q522 56 526 62Z" fill="var(--m3s)"/><rect x="493" y="71" width="24" height="12" fill="var(--bad)" opacity=".85"/><path d="M490.5 72L495.5 63L514.5 63L519.5 72Z" fill="var(--tree)"/><path d="M505 63L514.5 63L519.5 72L505 72Z" fill="var(--tree2)" opacity=".6"/><rect x="500" y="75" width="7" height="8" fill="var(--snow)"/></g>`;
  b+=`<path d="${nr}" fill="var(--m3)"/>`+fields('n',nr,88,['var(--m3)','var(--leaf)','var(--m3s)'],14);
  [[310,99],[430,93],[548,96]].forEach(([x,y],i)=>{b+=`<g class="sc-farm-sw" style="animation-delay:${-i*1.3}s"><rect x="${x-.7}" y="${y-2}" width="1.4" height="5" fill="var(--tree)"/><circle cx="${x}" cy="${y-5}" r="${5+i%2}" fill="${i%2?'var(--tree2)':'var(--tree)'}"/></g>`;});
  const env='M0 -18C11 -18 14 -8 10 0C8 4 4 8 2 10L-2 10C-4 8 -8 4 -10 0C-14 -8 -11 -18 0 -18Z';
  b+=`<g class="sc-farm-bl"><g transform="translate(446 46) scale(.7)"><clipPath id="sc-farm-bc"><path d="${env}"/></clipPath><path d="${env}" fill="var(--snow)"/><g clip-path="url(#sc-farm-bc)"><rect x="-9" y="-20" width="4" height="32" fill="var(--m3)"/><rect x="-2" y="-20" width="4" height="32" fill="var(--m3)"/><rect x="5" y="-20" width="4" height="32" fill="var(--m3)"/><rect x="-12" y="-2.5" width="24" height="3" fill="var(--orb)"/></g><path d="M-2 10L-2.5 13.5M2 10L2.5 13.5" stroke="var(--bird)" stroke-width=".6"/><rect x="-3" y="13.5" width="6" height="3.6" rx=".6" fill="var(--tree)"/></g></g>`;
  b+=`<g clip-path="url(#sc-farm-m)"><ellipse class="sc-farm-sh" cx="400" cy="94" rx="64" ry="9" fill="var(--m3s)" opacity=".4"/></g>`;
  b+=`<g class="sc-farm-cl"><ellipse cx="330" cy="24" rx="22" ry="5" fill="var(--snow)" opacity=".65"/><ellipse cx="342" cy="20" rx="13" ry="5" fill="var(--snow)" opacity=".65"/><ellipse cx="319" cy="21" rx="11" ry="4" fill="var(--snow)" opacity=".65"/></g>`;
  b+=kestrel(298,34,.8);
  const css=haloCss('farm')+`.sc-farm-sh{animation:sc-farm-sd 14s ease-in-out infinite alternate}@keyframes sc-farm-sd{from{transform:translateX(-110px)}to{transform:translateX(120px)}}.sc-farm-cl{animation:sc-farm-cd 26s ease-in-out infinite alternate}@keyframes sc-farm-cd{from{transform:translateX(-30px)}to{transform:translateX(150px)}}`+`.sc-farm-bl{animation:sc-farm-up 12s ease-in-out infinite alternate;transform-box:fill-box;transform-origin:50% 100%}@keyframes sc-farm-up{from{transform:translateY(7px) rotate(-2deg)}to{transform:translateY(-12px) rotate(2deg)}}.sc-farm-sw{animation:sc-farm-sy 7s ease-in-out infinite alternate;transform-origin:bottom;transform-box:fill-box}@keyframes sc-farm-sy{to{transform:skewX(4.5deg)}}`;
  return frame('farm',366,34,b,css);
}
SCENES.push({id:'desert',name:'Desert',moves:'Heat haze rises off the ground, a dust devil drifts past',draw:sceneDesert});
SCENES.push({id:'prairie',name:'Prairie',moves:'Windmill blades turn slowly, grass sways',draw:scenePrairie});
SCENES.push({id:'forest',name:'Forest',moves:'Mist drifts between the pines, fireflies glow at night',draw:sceneForest});
SCENES.push({id:'city',name:'City',moves:'A plane crosses, windows light up at night',draw:sceneCity});
SCENES.push({id:'lake',name:'Lake',moves:'A sailboat drifts, ripples roll across the water',draw:sceneLake});
SCENES.push({id:'farmland',name:'Farmland',moves:'A hot-air balloon rises and sinks gently',draw:sceneFarm});
})();
