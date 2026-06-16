// Service worker for Workspace Chat — handles background push notifications.
self.addEventListener('install', function (event) { self.skipWaiting(); });
self.addEventListener('activate', function (event) { event.waitUntil(self.clients.claim()); });

self.addEventListener('push', function (event) {
    let data = {};
    try { data = event.data ? event.data.json() : {}; }
    catch (e) { data = { title: 'Workspace Chat', body: event.data ? event.data.text() : '' }; }
    const title = data.title || 'Workspace Chat';
    const options = {
        body: data.body || '',
        icon: '/static/icon.svg',
        badge: '/static/icon.svg',
        data: { url: data.url || '/dashboard' },
        tag: 'workspace-chat'
    };
    event.waitUntil(self.registration.showNotification(title, options));
});

self.addEventListener('notificationclick', function (event) {
    event.notification.close();
    const target = (event.notification.data && event.notification.data.url) || '/dashboard';
    event.waitUntil(
        clients.matchAll({ type: 'window', includeUncontrolled: true }).then(function (list) {
            for (const c of list) { if (c.url.indexOf('/dashboard') !== -1 && 'focus' in c) return c.focus(); }
            if (clients.openWindow) return clients.openWindow(target);
        })
    );
});
