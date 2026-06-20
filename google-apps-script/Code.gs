/**
 * LS Mentor Slack Notification
 *
 * Sheet columns: Mentor Name | Email | Titile (or Title)
 * Sends each mentor a Slack DM with the title text.
 *
 * SETUP:
 * 1. Slack App: https://api.slack.com/apps → Create New App
 * 2. OAuth scopes (Bot Token): users:read.email, chat:write, im:write
 * 3. Install app to research-aitalk workspace → copy Bot User OAuth Token (xoxb-...)
 * 4. In this sheet: Extensions → Apps Script → paste this file
 * 5. Run "setupScriptProperties" once, paste your Slack token when prompted
 * 6. Run "sendMentorSlackNotifications" to test
 * 7. Run "createWeeklyTrigger" for every Monday 9:00 AM (Asia/Kolkata)
 */

var CONFIG = {
  SHEET_NAME: 'Sheet1',
  HEADER_ROW: 1,
  // Column headers (case-insensitive match)
  COL_NAME: 'Mentor Name',
  COL_EMAIL: 'Email',
  COL_TITLE: 'Titile', // sheet typo; "Title" also works
  COL_TITLE_ALT: 'Title',
  // Optional: post a summary to your private channel after DMs
  SUMMARY_CHANNEL_ID: 'C0BBY7CJRMY',
  POST_SUMMARY: true
};

function setupScriptProperties() {
  var ui = SpreadsheetApp.getUi();
  var tokenResponse = ui.prompt(
    'Slack Bot Token',
    'Paste Bot User OAuth Token (starts with xoxb-):',
    ui.ButtonSet.OK_CANCEL
  );
  if (tokenResponse.getSelectedButton() !== ui.Button.OK) return;

  PropertiesService.getScriptProperties().setProperty(
    'SLACK_BOT_TOKEN',
    tokenResponse.getResponseText().trim()
  );
  ui.alert('Saved! Now run sendMentorSlackNotifications to test.');
}

function createWeeklyTrigger() {
  deleteWeeklyTriggers_();
  ScriptApp.newTrigger('sendMentorSlackNotifications')
    .timeBased()
    .onWeekDay(ScriptApp.WeekDay.MONDAY)
    .atHour(9)
    .inTimezone('Asia/Kolkata')
    .create();
  SpreadsheetApp.getUi().alert('Weekly trigger set: every Monday 9:00 AM IST.');
}

function deleteWeeklyTriggers_() {
  ScriptApp.getProjectTriggers().forEach(function (trigger) {
    if (trigger.getHandlerFunction() === 'sendMentorSlackNotifications') {
      ScriptApp.deleteTrigger(trigger);
    }
  });
}

function sendMentorSlackNotifications() {
  var token = PropertiesService.getScriptProperties().getProperty('SLACK_BOT_TOKEN');
  if (!token) {
    throw new Error('SLACK_BOT_TOKEN missing. Run setupScriptProperties first.');
  }

  var sheet = SpreadsheetApp.getActiveSpreadsheet().getSheetByName(CONFIG.SHEET_NAME);
  if (!sheet) {
    throw new Error('Sheet not found: ' + CONFIG.SHEET_NAME);
  }

  var mentors = readMentorRows_(sheet);
  var results = { sent: 0, skipped: 0, failed: [] };

  mentors.forEach(function (mentor) {
    if (!mentor.email || !mentor.title) {
      results.skipped++;
      return;
    }

    try {
      var userId = slackLookupUserByEmail_(token, mentor.email);
      slackOpenDmAndSend_(token, userId, mentor.title);
      results.sent++;
      Utilities.sleep(1200); // Slack rate limit ~1 msg/sec
    } catch (err) {
      results.failed.push(mentor.name + ' (' + mentor.email + '): ' + err.message);
    }
  });

  if (CONFIG.POST_SUMMARY && CONFIG.SUMMARY_CHANNEL_ID) {
    var summary =
      '*Weekly mentor notifications*\n' +
      'Sent: ' + results.sent + '\n' +
      'Skipped (empty email/title): ' + results.skipped;
    if (results.failed.length) {
      summary += '\nFailed:\n• ' + results.failed.join('\n• ');
    }
    slackPostMessage_(token, CONFIG.SUMMARY_CHANNEL_ID, summary);
  }

  Logger.log(JSON.stringify(results, null, 2));
  return results;
}

function readMentorRows_(sheet) {
  var data = sheet.getDataRange().getValues();
  if (data.length <= CONFIG.HEADER_ROW) return [];

  var headers = data[CONFIG.HEADER_ROW - 1].map(function (h) {
    return String(h).trim().toLowerCase();
  });

  var nameIdx = headers.indexOf(CONFIG.COL_NAME.toLowerCase());
  var emailIdx = headers.indexOf(CONFIG.COL_EMAIL.toLowerCase());
  var titleIdx = headers.indexOf(CONFIG.COL_TITLE.toLowerCase());
  if (titleIdx === -1) titleIdx = headers.indexOf(CONFIG.COL_TITLE_ALT.toLowerCase());

  if (emailIdx === -1 || titleIdx === -1) {
    throw new Error('Required columns missing. Need Email and Titile/Title.');
  }

  var mentors = [];
  for (var r = CONFIG.HEADER_ROW; r < data.length; r++) {
    var row = data[r];
    mentors.push({
      name: nameIdx >= 0 ? String(row[nameIdx] || '').trim() : '',
      email: String(row[emailIdx] || '').trim(),
      title: String(row[titleIdx] || '').trim()
    });
  }
  return mentors;
}

function slackLookupUserByEmail_(token, email) {
  var url =
    'https://slack.com/api/users.lookupByEmail?email=' +
    encodeURIComponent(email);
  var response = slackRequest_(token, url, 'get');
  if (!response.ok) {
    throw new Error(response.error || 'users.lookupByEmail failed');
  }
  return response.user.id;
}

function slackOpenDmAndSend_(token, userId, text) {
  var open = slackRequest_(
    token,
    'https://slack.com/api/conversations.open',
    'post',
    { users: userId }
  );
  if (!open.ok) {
    throw new Error(open.error || 'conversations.open failed');
  }
  slackPostMessage_(token, open.channel.id, text);
}

function slackPostMessage_(token, channelId, text) {
  var response = slackRequest_(
    token,
    'https://slack.com/api/chat.postMessage',
    'post',
    { channel: channelId, text: text }
  );
  if (!response.ok) {
    throw new Error(response.error || 'chat.postMessage failed');
  }
  return response;
}

function slackRequest_(token, url, method, payload) {
  var options = {
    method: method,
    headers: { Authorization: 'Bearer ' + token },
    muteHttpExceptions: true
  };
  if (payload) {
    options.method = 'post';
    options.contentType = 'application/json; charset=utf-8';
    options.payload = JSON.stringify(payload);
  }

  var res = UrlFetchApp.fetch(url, options);
  return JSON.parse(res.getContentText());
}
