export function canonicalModelReference(
  providerName: string,
  model: string,
): string {
  return model.startsWith(`${providerName}/`)
    ? model
    : `${providerName}/${model}`;
}

export function displayModelName(
  model: string,
  providerName?: string | null,
): string {
  if (providerName && model.startsWith(`${providerName}/`)) {
    return model.slice(providerName.length + 1);
  }
  return model;
}

export function modelBelongsToProvider(
  model: string,
  providerName: string,
): boolean {
  if (!model) return false;
  return model.startsWith(`${providerName}/`);
}
