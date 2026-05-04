export type ConvergenceStampRow = {
  signal_id: string;
  convergence_bonus?: number | null;
  score_with_bonus?: number | null;
  band_with_bonus?: string | null;
};

export function shouldUseLitigationWindow(
  firstPassProfiles: Array<string | null | undefined>,
  hasLitigationInExtendedWindow: boolean,
): boolean {
  return firstPassProfiles.includes("litigation") ||
    hasLitigationInExtendedWindow;
}

export function shouldClearDisplacedWinner(
  row: ConvergenceStampRow,
  winnerId: string,
): boolean {
  return row.signal_id !== winnerId &&
    ((row.convergence_bonus ?? 0) > 0 ||
      row.score_with_bonus != null ||
      row.band_with_bonus != null);
}
