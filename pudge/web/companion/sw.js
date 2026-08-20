const SHELL_CACHE='pudge-companion-shell-v15';
const SHELL=[
  '/companion/',
  '/companion/app.js?v=15',
  '/companion/styles.css?v=15',
  '/companion/manifest.webmanifest',
  '/companion/icon.svg'
];
self.addEventListener('install',event=>{
  event.waitUntil(caches.open(SHELL_CACHE).then(cache=>cache.addAll(SHELL)));
  self.skipWaiting();
});
self.addEventListener('activate',event=>event.waitUntil(
  caches.keys().then(keys=>Promise.all(
    keys.filter(key=>key.startsWith('pudge-companion-shell-')&&key!==SHELL_CACHE).map(key=>caches.delete(key))
  )).then(()=>self.clients.claim())
));
self.addEventListener('fetch',event=>{
  const url=new URL(event.request.url);
  if(url.origin!==self.location.origin)return;
  if(url.pathname.startsWith('/api/')){
    event.respondWith(fetch(event.request));
    return;
  }
  if(!url.pathname.startsWith('/companion/'))return;
  const mutable=url.pathname==='/companion/'||url.pathname.endsWith('/index.html')||url.pathname.endsWith('/app.js')||url.pathname.endsWith('/styles.css');
  if(mutable){
    event.respondWith(fetch(event.request).then(response=>{
      const copy=response.clone();
      caches.open(SHELL_CACHE).then(cache=>cache.put(event.request,copy)).catch(()=>{});
      return response;
    }).catch(()=>caches.match(event.request)));
    return;
  }
  event.respondWith(caches.match(event.request).then(cached=>cached||fetch(event.request)));
});
