// Message dans le salon des mises à jour (webhook Discord), mis en forme comme les logs des bots.
// Variables : DISCORD_WEBHOOK (secret ; sans lui, rien n'est envoyé), TITLE, DESCRIPTION, URL,
// ACCENT (success | danger | info | warning).
const ACCENTS = { success: 0x3ba55c, danger: 0xed4245, info: 0x5865f2, warning: 0xf0b232 };

const { DISCORD_WEBHOOK, TITLE, DESCRIPTION = '', URL, ACCENT = 'info', GITHUB_REPOSITORY } = process.env;
if (!DISCORD_WEBHOOK) {
  console.log('DISCORD_WEBHOOK absent : notification ignorée.');
  process.exit(0);
}

const truncate = (text, max) => (text.length > max ? `${text.slice(0, max - 1)}…` : text);
const embed = {
  title: truncate(TITLE ?? 'Mise à jour', 256),
  description: truncate(DESCRIPTION, 4000),
  color: ACCENTS[ACCENT] ?? ACCENTS.info,
  ...(URL && { url: URL }),
  footer: { text: GITHUB_REPOSITORY ?? '' },
  timestamp: new Date().toISOString(),
};

const response = await fetch(DISCORD_WEBHOOK, {
  method: 'POST',
  headers: { 'Content-Type': 'application/json' },
  // Aucune mention ne notifie, même si un changelog en contient.
  body: JSON.stringify({ embeds: [embed], allowed_mentions: { parse: [] } }),
});
if (!response.ok) {
  // Une notification ratée ne fait pas échouer le workflow.
  console.log(`::warning::Discord a répondu ${response.status} : ${await response.text()}`);
}
