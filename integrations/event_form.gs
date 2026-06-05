/**
 * NYFurs Event Application  ->  FurBot intake
 * ------------------------------------------------------------------
 * Sends every form submission (answers + uploaded files) to FurBot's
 * signed /event-intake endpoint, which posts it to #event-post with
 * Accept / Decline buttons.
 *
 * SETUP (in the Google Form):
 *   1. Open the Form -> ⋮ menu -> "Script editor".
 *   2. Paste this whole file.
 *   3. Set ENDPOINT and SECRET below:
 *        ENDPOINT = your bot's public URL + "/event-intake"
 *                   (Railway -> service -> Settings -> Networking -> Generate Domain)
 *        SECRET   = the exact same value as the bot's EVENT_FORM_SECRET env var.
 *   4. Run `onEventSubmit` once manually to grant permissions (Drive + external requests).
 *   5. Triggers (clock icon) -> Add Trigger:
 *        function: onEventSubmit
 *        event source: From form
 *        event type: On form submit
 *
 * The signature scheme matches the bot: HMAC-SHA256 over "<timestamp>.<body>"
 * using SECRET, sent as X-Signature (hex) + X-Timestamp headers.
 */

var ENDPOINT = 'https://YOUR-BOT.up.railway.app/event-intake';
var SECRET = 'PASTE_THE_SAME_VALUE_AS_EVENT_FORM_SECRET';
var MAX_FILE_BYTES = 8 * 1024 * 1024; // skip files Discord can't accept (~8 MB)

function onEventSubmit(e) {
  var resp = e.response;
  var answers = [];
  var files = [];

  // Respondent email, if the form collects it.
  try {
    var email = resp.getRespondentEmail();
    if (email) answers.push({ q: 'Email Address', a: email });
  } catch (err) { /* email collection off */ }

  var items = resp.getItemResponses();
  for (var i = 0; i < items.length; i++) {
    var ir = items[i];
    var item = ir.getItem();
    var title = item.getTitle();

    if (item.getType() === FormApp.ItemType.FILE_UPLOAD) {
      var ids = ir.getResponse() || [];
      var names = [];
      for (var j = 0; j < ids.length; j++) {
        try {
          var file = DriveApp.getFileById(ids[j]);
          var blob = file.getBlob();
          names.push(file.getName());
          if (blob.getBytes().length <= MAX_FILE_BYTES) {
            files.push({
              field: title,
              filename: file.getName(),
              mime: blob.getContentType(),
              b64: Utilities.base64Encode(blob.getBytes())
            });
          }
        } catch (err2) { /* file unreadable; skip */ }
      }
      answers.push({ q: title, a: names.join(', ') });
    } else {
      var v = ir.getResponse();
      if (Object.prototype.toString.call(v) === '[object Array]') v = v.join(', ');
      answers.push({ q: title, a: v == null ? '' : String(v) });
    }
  }

  var payload = {
    submission_id: resp.getId(),
    submitted_at: new Date().toISOString(),
    answers: answers,
    files: files
  };

  var body = JSON.stringify(payload);
  var ts = String(Math.floor(Date.now() / 1000));
  var sigBytes = Utilities.computeHmacSha256Signature(ts + '.' + body, SECRET);
  var sig = sigBytes
    .map(function (b) { return ('0' + (b & 0xff).toString(16)).slice(-2); })
    .join('');

  var res = UrlFetchApp.fetch(ENDPOINT, {
    method: 'post',
    contentType: 'application/json',
    payload: body,
    muteHttpExceptions: true,
    headers: { 'X-Signature': sig, 'X-Timestamp': ts }
  });

  // Surface failures in the Apps Script execution log for debugging.
  if (res.getResponseCode() >= 300) {
    Logger.log('Event intake failed: HTTP %s %s', res.getResponseCode(), res.getContentText());
  }
}
