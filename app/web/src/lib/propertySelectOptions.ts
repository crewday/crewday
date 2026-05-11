export interface PropertySelectOptionSource {
  id: string;
  name: string;
  city?: string | null;
  timezone?: string | null;
}

export interface PropertySelectOption {
  value: string;
  label: string;
  secondaryText?: string;
  searchText?: string;
}

export function propertySelectOption(property: PropertySelectOptionSource): PropertySelectOption {
  return {
    value: property.id,
    label: property.name,
    secondaryText: property.city || property.timezone || undefined,
    searchText: [property.name, property.city, property.timezone].filter(Boolean).join(" "),
  };
}
