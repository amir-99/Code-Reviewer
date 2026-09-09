// Both filters apply independently; old findings have unknown impact.
export function matchesFinding(finding, disposition, impact) {
  return (disposition === 'all' || finding.severity === disposition)
    && (impact === 'all' || (finding.impact_level || 'unknown') === impact);
}
