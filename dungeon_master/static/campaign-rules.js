"use strict";

let campaignRuleChoices = [], ruleChoiceCampaign = null, expectedRuleChoice = null, savingCampaignRules = false;
let campaignRuleLoad = 0;
function renderCampaignRules() {
  if (!current) return;
  q("#campaign-rule-set-name").textContent = current.rule_set ? `${current.rule_set.name}${current.rule_set.ready ? "" : " · needs attention"}` : "No set selected";
}
function describeCampaignRuleChoice() {
  const id = q("#campaign-rules-choice").value;
  const group = campaignRuleChoices.find((item) => item.id === id);
  q("#campaign-rule-books").textContent = group ? group.books.map((book) => `${book.title}${book.available ? "" : " · not ready in Rules"}`).join("\n") || "This set has no books. Edit it in Materials first." : "Choose a set to give this campaign a specific group of rulebooks.";
  q("#save-campaign-rules").disabled = savingCampaignRules || Boolean(id && !group?.ready);
}
async function openCampaignRules() {
  if (!current || savingCampaignRules) return;
  const load = ++campaignRuleLoad;
  ruleChoiceCampaign = current.id;
  q("#campaign-rules-for").textContent = current.name;
  q("#campaign-rules-error").hidden = true;
  q("#campaign-rules-choice").innerHTML = '<option value="">Loading rulebook sets…</option>';
  q("#save-campaign-rules").disabled = true;
  q("#campaign-rules-dialog").showModal();
  try {
    const [{rule_sets}, latest] = await Promise.all([request("/api/rule-sets"), request(`/api/campaigns/${ruleChoiceCampaign}`)]);
    if (load !== campaignRuleLoad || !q("#campaign-rules-dialog").open) return;
    campaignRuleChoices = rule_sets;
    expectedRuleChoice = latest.rule_set_id;
    q("#campaign-rules-choice").innerHTML = '<option value="">No rulebook set selected</option>' + rule_sets.map((group) => `<option value="${group.id}" ${group.ready ? "" : "disabled"}>${esc(group.name)} · ${group.book_ids.length} books${group.ready ? "" : " · needs attention"}</option>`).join("");
    q("#campaign-rules-choice").value = expectedRuleChoice || "";
    describeCampaignRuleChoice();
  } catch (error) { q("#campaign-rules-error").textContent = error.message; q("#campaign-rules-error").hidden = false; }
}
document.querySelectorAll("[data-open-rules]").forEach((button) => button.addEventListener("click", openCampaignRules));
q("#close-campaign-rules").addEventListener("click", () => q("#campaign-rules-dialog").close());
q("#campaign-rules-choice").addEventListener("change", describeCampaignRuleChoice);
q("#campaign-rules-dialog").addEventListener("cancel", (event) => { if (savingCampaignRules) event.preventDefault(); });
q("#campaign-rules-form").addEventListener("submit", async (event) => {
  event.preventDefault();
  if (savingCampaignRules || !ruleChoiceCampaign) return;
  const campaignId = ruleChoiceCampaign;
  savingCampaignRules = true; q("#close-campaign-rules").disabled = true; describeCampaignRuleChoice();
  q("#campaign-rules-error").hidden = true;
  try {
    await write(`/api/campaigns/${campaignId}/rules`, {rule_set_id:q("#campaign-rules-choice").value || null, expected_rule_set_id:expectedRuleChoice}, "PUT");
    if (current?.id === campaignId) await loadCampaign(campaignId);
    q("#campaign-rules-dialog").close();
    announce("Campaign rulebook set saved.");
  } catch (error) { q("#campaign-rules-error").textContent = error.message; q("#campaign-rules-error").hidden = false; }
  finally { savingCampaignRules = false; q("#close-campaign-rules").disabled = false; describeCampaignRuleChoice(); }
});
if (current) renderCampaignRules();
