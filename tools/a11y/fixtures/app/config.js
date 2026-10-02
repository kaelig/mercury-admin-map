// What a test variant changes. A variant copies this folder to <tmp>/<name>/app and rewrites this file,
// so the variant serves the same paths on its own origin (recipes.test.mjs).
export default {
  // The word before a transaction's relative date: AE2 renames it, so the drawer's trigger changes name.
  paidWord: 'Paid',
  // Days since the Blue Bottle Coffee payment: 1 renders "Yesterday", 2 renders "2 days ago".
  daysAgo: 1,
  // 'limits' opens the Edit card limits dialog; 'freeze' makes the same click open Freeze card.
  editLimitsOpens: 'limits',
  // true sends /app/reports to /app/dashboard, like a page the demo retired.
  reportsRedirect: false,
};
