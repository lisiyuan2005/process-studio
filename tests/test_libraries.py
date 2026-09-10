from process_studio.libraries import RecipeLibrary
from process_studio.models import MaterialResponse, ProcessType, Recipe


def test_recipe_excel_round_trip(tmp_path) -> None:
    recipe = Recipe(
        name="BOE 6:1",
        process_type=ProcessType.ETCH,
        tool="Wet Bench 1",
        parameters={"time_min": 1.0, "temperature_c": 25.0, "target": 0.08},
        material_responses={
            "SiO2": MaterialResponse("SiO2", 0.08),
            "Si": MaterialResponse("Si", 0.0, stop_layer=True),
        },
    )
    path = tmp_path / "recipes.xlsx"
    RecipeLibrary([recipe]).export_excel(path)
    restored = RecipeLibrary.import_excel(path)
    loaded = next(iter(restored.recipes.values()))

    assert loaded.name == recipe.name
    assert loaded.tool == recipe.tool
    assert loaded.parameters["time_min"] == 1.0
    assert loaded.parameters["temperature_c"] == 25.0
    assert loaded.material_responses["SiO2"].rate_um_per_min == 0.08
    assert loaded.material_responses["Si"].stop_layer
