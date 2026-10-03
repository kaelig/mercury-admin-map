// Results of the custom checks (tabwalk, dialog, status, target) in schemas.md's result shape. Each
// custom rule's WCAG criteria come from checks.json's customCriteria.
import { join } from 'node:path';
import { ROOT, readJson } from './canon.mjs';

const CUSTOM_CRITERIA = readJson(join(ROOT, 'tools/a11y/checks.json')).customCriteria;

export const customResult = (check, rule, basis, occurrence, evidence) => ({
  check, rule, basis, criteria: CUSTOM_CRITERIA[rule], messageKey: null, occurrence, evidence,
});
