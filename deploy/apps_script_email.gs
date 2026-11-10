/**
 * Email relay for cambridge-ma-parking-checks (for hosts that block outbound SMTP).
 *
 * Setup (once):
 *  1. Go to https://script.google.com, signed in as the Gmail account that should send alerts,
 *     click New project, delete the sample code, and paste this whole file.
 *  2. Set SECRET below to EMAIL_WEBHOOK_SECRET from the server's .env, and list the recipients
 *     in ALLOWED_TO (lower case). The relay refuses any other address.
 *  3. Select the function `testSend` in the toolbar and click Run. Approve the permission
 *     prompt (Advanced -> Go to project). You should get a test email.
 *  4. Deploy -> New deployment -> type Web app. Execute as: Me. Who has access: Anyone.
 *     Deploy, then copy the Web app URL (https://script.google.com/macros/s/.../exec) into
 *     EMAIL_WEBHOOK_URL in the server's .env.
 *
 * After editing this script later, use Deploy -> Manage deployments -> Edit -> Version: New
 * version, so the same URL serves the new code.
 */

const SECRET = 'paste-EMAIL_WEBHOOK_SECRET-here';
const ALLOWED_TO = ['you@example.com'];
const SENDER_NAME = 'parking-checks';

function doPost(e) {
  let req;
  try {
    req = JSON.parse(e.postData.contents);
  } catch (err) {
    return reply_({ ok: false, error: 'bad json' });
  }
  if (!req || typeof req.secret !== 'string' || req.secret !== SECRET || SECRET.indexOf('paste-') === 0) {
    return reply_({ ok: false, error: 'unauthorized' });
  }
  const to = (Array.isArray(req.to) ? req.to : [req.to])
    .map(function (a) { return String(a || '').trim().toLowerCase(); })
    .filter(function (a) { return ALLOWED_TO.indexOf(a) !== -1; });
  if (to.length === 0) {
    return reply_({ ok: false, error: 'no allowed recipients' });
  }
  MailApp.sendEmail({
    to: to.join(','),
    subject: String(req.subject || 'parking-checks alert').slice(0, 250),
    body: String(req.body || '').slice(0, 20000),
    name: SENDER_NAME,
  });
  return reply_({ ok: true, sent: to.length, remaining_quota: MailApp.getRemainingDailyQuota() });
}

function doGet() {
  return reply_({ ok: true, service: 'parking-checks email relay' });
}

function testSend() {
  MailApp.sendEmail(ALLOWED_TO[0], '[parking] relay test', 'The Apps Script email relay works.', { name: SENDER_NAME });
}

function reply_(obj) {
  return ContentService.createTextOutput(JSON.stringify(obj)).setMimeType(ContentService.MimeType.JSON);
}
