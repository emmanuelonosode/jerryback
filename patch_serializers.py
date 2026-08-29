import re
filepath = "/Users/officialbookone/Desktop/Jerry/backend/apps/properties/serializers.py"
with open(filepath, "r") as f:
    content = f.read()

# Add to PublicPropertyDetailSerializer
search_str = '        fields = [\n            *PublicPropertyListSerializer.Meta.fields,\n            "description", "type", "year_built", "neighborhood",\n            "pet_policy", "accessibility_features",\n            "parking", "laundry", "hvac", "flooring", "appliances",\n            "tour_3d_url", "tour_video_url", "last_verified_at",\n            "images", "fees", "amenities",\n        ]'

replace_str = '        fields = [\n            *PublicPropertyListSerializer.Meta.fields,\n            "description", "type", "year_built", "neighborhood",\n            "pet_policy", "accessibility_features",\n            "parking", "laundry", "hvac", "flooring", "appliances",\n            "tour_3d_url", "tour_video_url", "last_verified_at",\n            "images", "fees", "amenities",\n            "schools", "raw_fees", "office_info", "floor_plans",\n        ]'

content = content.replace(search_str, replace_str)
with open(filepath, "w") as f:
    f.write(content)
print("patched")
