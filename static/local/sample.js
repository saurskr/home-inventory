// Sample data for the demo: one made-up home with rooms, items, insurance and a few stale prices.

export function sampleData(todayYmd) {
  const [y, m, d] = todayYmd.split("-").map(Number);
  const ago = days => { const t = new Date(y, m - 1, d - days); return `${t.getFullYear()}-${String(t.getMonth() + 1).padStart(2, "0")}-${String(t.getDate()).padStart(2, "0")}`; };
  const item = (name, brand, model, type, desc, paid, rep, boughtDays, cond, repDays, pic, url = "") => ({
    fields: {name, brand, model_number: model, item_type: type, description: desc, purchased_value: paid, replacement_value: rep,
      purchased_date: ago(boughtDays), purchased_condition: cond, similar_url: url},
    replacement_date: repDays === null ? null : ago(repDays), picture: pic});
  const renew = new Date(y, m - 1, d + 80);
  return {
    home: {name: "Maple Street House", street: "412 Maple Street", street2: "", city: "Springfield", state: "IL", zip: "62704"},
    insurance: {insurer: "Prairie Mutual", website: "https://www.example.com/prairie-mutual", policy_number: "HO-4471-209",
      renewal_date: `${renew.getFullYear()}-${String(renew.getMonth() + 1).padStart(2, "0")}-${String(renew.getDate()).padStart(2, "0")}`,
      dwelling_coverage: 385000, contents_coverage: 150000, liability_coverage: 300000, deductible: 1000, annual_premium: 1840,
      contact: "1-800-555-0142", notes: "Sample data. Not a real policy.",
      riders: [{name: "Jewelry and watches", limit: 10000}, {name: "Home office equipment", limit: 8000}]},
    rooms: [
      {name: "Kitchen", description: "Open plan, island with seating", items: [
        item("Stand Mixer", "KitchenAid", "KSM150PSER", "Appliance", "Empire red 5 quart tilt-head stand mixer with three attachments.", 329, 379.99, 900, "New", 20, {label: "🍰", color: "#c0392b"}, "https://www.example.com/products/stand-mixer"),
        item("Pressure Cooker", "Instant Pot", "DUO60", "Appliance", "6 quart 7-in-1 programmable pressure cooker, stainless steel.", 79, 89.99, 500, "New", 430, {label: "🍲", color: "#7f8c8d"}),
        item("Espresso Machine", "Breville", "BES870XL", "Appliance", "Semi-automatic espresso machine with built-in conical burr grinder.", 549, 599.95, 700, "Refurbished", 35, {label: "☕", color: "#6d4c41"}),
        item("Blender", "Vitamix", "E310", "Appliance", "Explorian series 48 oz blender, black.", 349, 349.95, 300, "New", 15, {label: "🥤", color: "#34495e"}),
        item("Microwave", "Panasonic", "NN-SN966S", "Appliance", "Countertop inverter microwave, 1.3 cubic feet, stainless.", 189, 179, 1500, "New", 600, {label: "♨️", color: "#95a5a6"}),
        item("Knife Block Set", "Wüsthof", "1090170602", "Collectible", "Classic 7-piece knife set with oak block.", 299, 229, 2000, "New", null, {label: "🔪", color: "#8d6e63"}),
      ]},
      {name: "Living Room", description: "", items: [
        item("Sofa", "West Elm", "Harmony 84", "Furniture", "Three-seat sofa, performance velvet, slate grey.", 1899, 2149, 1100, "New", 60, {label: "🛋️", color: "#546e7a"}),
        item("Television", "Samsung", "QN65Q80C", "Electronics", "65 inch QLED 4K smart television with wall mount.", 1299, 999, 800, "New", 45, {label: "📺", color: "#263238"}),
        item("Bookcase", "", "", "Furniture", "Solid oak five-shelf bookcase, 72 inches tall.", 450, 520, 2400, "Used - good", 700, {label: "📚", color: "#a1887f"}),
        item("Coffee Table", "CB2", "Pebble", "Furniture", "Round marble-top coffee table with brass base.", 599, 649, 1000, "New", 50, {label: "🪑", color: "#bdbdbd"}),
        item("Vinyl Record Collection", "", "", "Media", "About 140 records, mostly 1960s to 1980s jazz and rock.", 2200, 3500, 5000, "Used - good", null, {label: "💿", color: "#37474f"}),
      ]},
      {name: "Master Bedroom", description: "", items: [
        item("Bed Frame", "Article", "Sven King", "Furniture", "King size upholstered bed frame with headboard.", 1099, 1199, 1300, "New", 90, {label: "🛏️", color: "#8e6e53"}),
        item("Dresser", "IKEA", "Hemnes 8-drawer", "Furniture", "Eight-drawer dresser, white stain.", 399, 449, 1400, "New", 120, {label: "🗄️", color: "#ecf0f1"}),
        item("Nightstand", "IKEA", "Hemnes", "Furniture", "Two-drawer nightstand, white stain.", 129, 139, 1400, "New", 120, {label: "🪑", color: "#ecf0f1"}),
        item("Wristwatch", "Seiko", "SRPD", "Collectible", "Automatic diver style watch with steel bracelet.", 295, 349, 600, "New", 70, {label: "⌚", color: "#1f618d"}),
      ]},
      {name: "Garage", description: "Workbench along the back wall", items: [
        item("Lawn Mower", "Honda", "HRX217", "Appliance", "21 inch self-propelled gas lawn mower.", 599, 699, 1800, "New", 500, {label: "🌱", color: "#27ae60"}),
        item("Cordless Drill Set", "DeWalt", "DCK240C2", "Appliance", "20V MAX drill and impact driver kit with two batteries.", 229, 249, 900, "New", 40, {label: "🔧", color: "#f1c40f"}),
        item("Road Bike", "Trek", "Domane AL 2", "Collectible", "Aluminum endurance road bike, size 56, black.", 1050, 1199, 1200, "New", 400, {label: "🚲", color: "#2c3e50"}),
      ]},
    ],
  };
}
