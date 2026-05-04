import {
  shouldClearDisplacedWinner,
  shouldUseLitigationWindow,
} from "./convergence-window.ts";

function assert(condition: unknown, message: string): asserts condition {
  if (!condition) throw new Error(message);
}

Deno.test("shouldUseLitigationWindow expands when litigation is only in extended window", () => {
  assert(
    shouldUseLitigationWindow(
      ["binary_catalyst", "takeover_candidate"],
      true,
    ) === true,
    "old litigation signals from days 15-30 must trigger the 30d window",
  );
});

Deno.test("shouldUseLitigationWindow keeps standard window when no litigation is present", () => {
  assert(
    shouldUseLitigationWindow(
      ["binary_catalyst", "takeover_candidate"],
      false,
    ) === false,
    "non-litigation groups should stay on the 14d window",
  );
});

Deno.test("shouldClearDisplacedWinner clears stale single-winner stamps", () => {
  assert(
    shouldClearDisplacedWinner({
      signal_id: "old-single",
      convergence_bonus: 0,
      score_with_bonus: 34,
      band_with_bonus: "watchlist",
    }, "new-winner") === true,
    "single winners have bonus=0 but still carry display stamps",
  );
});

Deno.test("shouldClearDisplacedWinner ignores unstamped non-winners and current winner", () => {
  assert(
    shouldClearDisplacedWinner(
      { signal_id: "plain", convergence_bonus: 0 },
      "winner",
    ) === false,
    "plain siblings without convergence stamps do not need clearing",
  );
  assert(
    shouldClearDisplacedWinner({
      signal_id: "winner",
      convergence_bonus: 5,
      score_with_bonus: 40,
      band_with_bonus: "immediate",
    }, "winner") === false,
    "current winner must never be cleared",
  );
});
