// MailPilot Service Worker — gestion des notifications de rappel RDV
'use strict';

self.addEventListener('install',  () => self.skipWaiting());
self.addEventListener('activate', e  => e.waitUntil(self.clients.claim()));

// Clic sur une notification → ouvrir / mettre au premier plan l'onglet agenda
self.addEventListener('notificationclick', e => {
  e.notification.close();
  const rdvId = e.notification.data?.rdvId;
  e.waitUntil(
    self.clients.matchAll({ type: 'window', includeUncontrolled: true }).then(list => {
      for (const c of list) {
        if (c.url.includes('/agenda')) { c.focus(); return; }
      }
      // Aucun onglet agenda ouvert : en ouvrir un
      const url = e.notification.data?.agendaUrl || '/dashboard';
      return self.clients.openWindow(url);
    })
  );
});
