#pragma once

#include "CoreMinimal.h"
#include "GameFramework/Actor.h"
#include "GTAIVTrafficData.h"
#include "GTAIVTrafficCar.generated.h"

class UStaticMeshComponent;

/** A GTA IV car driven by AGTAIVTrafficManager (moved kinematically along the lanes). */
UCLASS(NotPlaceable)
class GTAIVTRAFFIC_API AGTAIVTrafficCar : public AActor
{
	GENERATED_BODY()

public:
	AGTAIVTrafficCar();

	/** Builds the body and wheels and applies a paint combo (per-car custom primitive data). */
	void Setup(const FGTAIVVehicleDef& Def, const FGTAIVPaint& Paint);

	/** Rolls the wheels by DistanceCm travelled and steers the front ones. */
	void UpdateWheels(float DistanceCm, float SteerDegrees);

	UPROPERTY(VisibleAnywhere, Category = "GTA IV")
	TObjectPtr<USceneComponent> Root;

	UPROPERTY(VisibleAnywhere, Category = "GTA IV")
	TObjectPtr<UStaticMeshComponent> Body;

	UPROPERTY(VisibleAnywhere, Category = "GTA IV")
	TArray<TObjectPtr<UStaticMeshComponent>> WheelComponents;

	FName VehicleName;

private:
	TArray<FGTAIVWheel> WheelDefs;
	TArray<float> WheelRoll;
};
