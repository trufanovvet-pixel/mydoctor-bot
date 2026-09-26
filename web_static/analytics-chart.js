(() => {
  const data = JSON.parse(document.getElementById('analytics-series').textContent);
  const metric = document.getElementById('analytics-metric');
  const host = document.getElementById('analytics-chart');
  const ns = 'http://www.w3.org/2000/svg';
  const node = (name, attrs, text) => {const n=document.createElementNS(ns,name); for(const [k,v] of Object.entries(attrs))n.setAttribute(k,v); if(text!==undefined)n.textContent=text; return n;};
  function draw() {
    host.replaceChildren();
    const values = data.map(d=>metric.value.startsWith('revenue:') ? (d.revenue[metric.value.split(':')[1]]||0) : d[metric.value]);
    const max = Math.max(1,...values); const W=840,H=250,L=58,R=18,T=28,B=35;
    const svg=node('svg',{viewBox:`0 0 ${W} ${H}`,role:'img','aria-label':metric.selectedOptions[0].textContent});
    for(let i=0;i<=4;i++){
      const y=T+(H-T-B)*i/4;
      svg.append(node('line',{x1:L,y1:y,x2:W-R,y2:y,stroke:'#e6eef2'}));
      svg.append(node('text',{x:L-10,y:y+4,'text-anchor':'end'},new Intl.NumberFormat('ru',{maximumFractionDigits:1}).format(max*(1-i/4))));
    }
    const step=(W-L-R)/Math.max(1,data.length);const bw=Math.min(32,step*.64);
    data.forEach((d,i)=>{const x=L+step*i+step/2;const h=(H-T-B)*values[i]/max;
      const bar=node('rect',{x:x-bw/2,y:H-B-h,width:bw,height:h,rx:Math.min(4,bw/2),fill:'#07938e'});
      bar.append(node('title',{},`${d.label}: ${values[i]}`));svg.append(bar);
      if(i===0||i===data.length-1||i%Math.max(1,Math.ceil(data.length/6))===0){
        const label=d.label.includes(' ')?d.label.split(' ')[1]:d.label.slice(5);
        svg.append(node('text',{x,y:H-10,'text-anchor':'middle'},label));
      }
    });
    if(!values.some(Boolean))svg.append(node('text',{x:W/2,y:H/2,'text-anchor':'middle'},'За выбранный период событий пока нет'));
    host.append(svg);
    document.getElementById('analytics-chart-note').textContent='Наведите на столбец, чтобы увидеть значение. Точные цифры доступны в таблице ниже.';
  }
  metric.addEventListener('change',draw);draw();
})();
