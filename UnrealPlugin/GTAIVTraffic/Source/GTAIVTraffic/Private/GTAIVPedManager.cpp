#include "GTAIVPedManager.h"

#include "AIController.h"
#include "Components/CapsuleComponent.h"
#include "Components/ChildActorComponent.h"
#include "Components/SkeletalMeshComponent.h"
#include "Engine/SkeletalMesh.h"
#include "Engine/World.h"
#include "GameFramework/Character.h"
#include "GameFramework/CharacterMovementComponent.h"
#include "Camera/PlayerCameraManager.h"
#include "Kismet/GameplayStatics.h"
#include "UObject/UnrealType.h"

namespace
{
	const float CellSize = 10000.f;

	FIntPoint CellOf(const FVector& P)
	{
		return FIntPoint(FMath::FloorToInt(P.X / CellSize), FMath::FloorToInt(P.Y / CellSize));
	}
}

AGTAIVPedManager::AGTAIVPedManager()
{
	PrimaryActorTick.bCanEverTick = true;
	RootComponent = CreateDefaultSubobject<USceneComponent>(TEXT("Root"));
}

void AGTAIVPedManager::BeginPlay()
{
	Super::BeginPlay();
	Rng.Initialize(FMath::Rand());
	BuildCaches();
}

void AGTAIVPedManager::EndPlay(const EEndPlayReason::Type Reason)
{
	ClearPeds();
	Super::EndPlay(Reason);
}

void AGTAIVPedManager::ClearPeds()
{
	for (int32 i = Agents.Num() - 1; i >= 0; --i)
	{
		RemoveAgent(i);
	}
}

void AGTAIVPedManager::BuildCaches()
{
	Adjacent.Reset();
	Grid.Reset();
	if (!Network)
	{
		return;
	}
	const int32 N = Network->Nodes.Num();
	Adjacent.SetNum(N);
	for (int32 a = 0; a < N; ++a)
	{
		const int32 First = Network->FirstLink.IsValidIndex(a) ? Network->FirstLink[a] : 0;
		const int32 Count = Network->LinkCount.IsValidIndex(a) ? Network->LinkCount[a] : 0;
		for (int32 k = First; k < First + Count && k < Network->LinkTarget.Num(); ++k)
		{
			const int32 b = Network->LinkTarget[k];
			if (b < 0 || b >= N || b == a)
			{
				continue;
			}
			// pedestrians use one-way streets in both directions
			Adjacent[a].AddUnique(b);
			Adjacent[b].AddUnique(a);
		}
	}
	for (int32 a = 0; a < N; ++a)
	{
		if (Adjacent[a].Num() > 0)
		{
			Grid.FindOrAdd(CellOf(Network->Nodes[a])).Add(a);
		}
	}
}

int32 AGTAIVPedManager::LanesOut(int32 From, int32 To) const
{
	const int32 First = Network->FirstLink.IsValidIndex(From) ? Network->FirstLink[From] : 0;
	const int32 Count = Network->LinkCount.IsValidIndex(From) ? Network->LinkCount[From] : 0;
	for (int32 k = First; k < First + Count && k < Network->LinkTarget.Num(); ++k)
	{
		if (Network->LinkTarget[k] == To)
		{
			return Network->LinkLanes.IsValidIndex(k) ? Network->LinkLanes[k] : 1;
		}
	}
	return 0;
}

FVector AGTAIVPedManager::Sidewalk(int32 From, int32 To, float Side, bool bAtEnd) const
{
	const FVector A = Network->Nodes[From];
	const FVector B = Network->Nodes[To];
	const FVector Dir = (B - A).GetSafeNormal2D();
	const FVector Right(-Dir.Y, Dir.X, 0.f);   // Unreal is left-handed: (-y, x) is to the right
	const int32 Out = LanesOut(From, To);
	const int32 Back = LanesOut(To, From);
	float Half;
	if (Out > 0 && Back > 0)
	{
		Half = 50.f + (Side > 0.f ? Out : Back) * LaneWidth;  // two-way: lanes either side of the centre
	}
	else
	{
		Half = FMath::Max(1, Out + Back) * LaneWidth * 0.5f;  // one-way: lanes across the whole road
	}
	return (bAtEnd ? B : A) + Right * Side * (Half + SidewalkOffset);
}

bool AGTAIVPedManager::FindGround(const FVector& Point, FVector& OutGround) const
{
	FHitResult Hit;
	FCollisionQueryParams Params(SCENE_QUERY_STAT(GTAIVPedGround), false);
	for (const FPed& P : Agents)
	{
		if (P.Pawn.IsValid())
		{
			Params.AddIgnoredActor(P.Pawn.Get());
		}
	}
	const FVector Start = Point + FVector(0, 0, 400.f);
	const FVector End = Point - FVector(0, 0, 1500.f);
	// world geometry a character can stand on (the Visibility channel also hits things
	// pawns walk through)
	FCollisionObjectQueryParams Objects;
	Objects.AddObjectTypesToQuery(ECC_WorldStatic);
	Objects.AddObjectTypesToQuery(ECC_WorldDynamic);
	if (GetWorld()->LineTraceSingleByObjectType(Hit, Start, End, Objects, Params))
	{
		OutGround = Hit.ImpactPoint;
		return true;
	}
	return false;
}

bool AGTAIVPedManager::TrySpawn(const FVector& PlayerLoc)
{
	if (!Network || !PedClass || Adjacent.Num() == 0)
	{
		return false;
	}
	// a random street near the player, outside the minimum distance
	const FIntPoint C = CellOf(PlayerLoc);
	const int32 R = FMath::CeilToInt(SpawnRadius / CellSize);
	TArray<int32> Candidates;
	for (int32 dx = -R; dx <= R; ++dx)
	{
		for (int32 dy = -R; dy <= R; ++dy)
		{
			if (const TArray<int32>* Cell = Grid.Find(C + FIntPoint(dx, dy)))
			{
				Candidates.Append(*Cell);
			}
		}
	}
	if (Candidates.Num() == 0)
	{
		return false;
	}
	for (int32 Attempt = 0; Attempt < 8; ++Attempt)
	{
		const int32 From = Candidates[Rng.RandRange(0, Candidates.Num() - 1)];
		const TArray<int32>& Next = Adjacent[From];
		const int32 To = Next[Rng.RandRange(0, Next.Num() - 1)];
		const float Side = Rng.FRand() < 0.5f ? 1.f : -1.f;
		const float T = Rng.FRand();
		const FVector P = FMath::Lerp(Sidewalk(From, To, Side, false), Sidewalk(From, To, Side, true), T);
		const float D = FVector::Dist2D(P, PlayerLoc);
		if (D < MinSpawnDistance || D > SpawnRadius)
		{
			continue;
		}
		FVector Ground;
		if (!FindGround(P, Ground) || FMath::Abs(Ground.Z - P.Z) > 600.f)
		{
			continue;  // no floor here, or the sidewalk point landed on a roof or under a bridge
		}
		float HalfHeight = 90.f;
		if (const ACharacter* CDO = Cast<ACharacter>(PedClass->GetDefaultObject()))
		{
			HalfHeight = CDO->GetCapsuleComponent()->GetScaledCapsuleHalfHeight();
		}
		const FVector Dir = (Network->Nodes[To] - Network->Nodes[From]).GetSafeNormal2D();
		FActorSpawnParameters SP;
		SP.SpawnCollisionHandlingOverride = ESpawnActorCollisionHandlingMethod::AdjustIfPossibleButDontSpawnIfColliding;
		APawn* Pawn = GetWorld()->SpawnActor<APawn>(PedClass, Ground + FVector(0, 0, HalfHeight + 5.f), Dir.Rotation(), SP);
		if (!Pawn)
		{
			continue;
		}
		// a plain AI controller: no behaviour tree or state tree, the manager steers
		AController* Ctrl = GetWorld()->SpawnActor<AAIController>(AAIController::StaticClass(), Pawn->GetActorLocation(), Pawn->GetActorRotation());
		if (Pawn->GetController())
		{
			Pawn->GetController()->UnPossess();
		}
		if (Ctrl)
		{
			Ctrl->Possess(Pawn);
		}
		SetWalkGait(Pawn);
		FPed Ped;
		Ped.Pawn = Pawn;
		Ped.Controller = Ctrl;
		Ped.From = From;
		Ped.To = To;
		Ped.Side = Side;
		Ped.Target = Sidewalk(From, To, Side, true);
		Ped.LastLocation = Pawn->GetActorLocation();
		Ped.GroundZ = Ground.Z;
		Agents.Add(Ped);
		return true;
	}
	return false;
}

void AGTAIVPedManager::PickNext(FPed& P)
{
	const TArray<int32>& Next = Adjacent[P.To];
	TArray<int32> Choices;
	for (int32 n : Next)
	{
		if (n != P.From)
		{
			Choices.Add(n);
		}
	}
	if (Choices.Num() == 0)
	{
		// dead end: turn round and walk back along the other side
		Swap(P.From, P.To);
		P.Side = -P.Side;
	}
	else
	{
		P.From = P.To;
		P.To = Choices[Rng.RandRange(0, Choices.Num() - 1)];
	}
	P.Target = Sidewalk(P.From, P.To, P.Side, true);
}

void AGTAIVPedManager::SetWalkGait(APawn* Pawn) const
{
	if (!bWalk || !Pawn)
	{
		return;
	}
	// GASP: CharacterInputState is a Blueprint struct whose members carry GUID suffixes,
	// so match them by prefix.
	for (TFieldIterator<FStructProperty> It(Pawn->GetClass()); It; ++It)
	{
		if (!It->GetName().StartsWith(TEXT("CharacterInputState")))
		{
			continue;
		}
		void* Data = It->ContainerPtrToValuePtr<void>(Pawn);
		for (TFieldIterator<FBoolProperty> B(It->Struct); B; ++B)
		{
			const FString Name = B->GetName();
			if (Name.StartsWith(TEXT("WantsToWalk")))
			{
				B->SetPropertyValue_InContainer(Data, true);
			}
			else if (Name.StartsWith(TEXT("WantsToSprint")))
			{
				B->SetPropertyValue_InContainer(Data, false);
			}
		}
	}
}

void AGTAIVPedManager::DressPlayer(APawn* Player) const
{
	// GASP shows a visual override as a child actor of the character; when it's our GTA ped
	// Blueprint, give the player PlayerMesh instead of a random ped
	TArray<UChildActorComponent*> ChildComps;
	Player->GetComponents(ChildComps);
	for (UChildActorComponent* C : ChildComps)
	{
		AActor* Child = C->GetChildActor();
		if (!Child || !Child->GetClass()->GetName().Contains(TEXT("GTAIV_Ped")))
		{
			continue;
		}
		if (USkeletalMeshComponent* Mesh = Child->FindComponentByClass<USkeletalMeshComponent>())
		{
			if (Mesh->GetSkeletalMeshAsset() != PlayerMesh)
			{
				Mesh->SetSkeletalMeshAsset(PlayerMesh);
			}
		}
	}
}

void AGTAIVPedManager::RemoveAgent(int32 Index)
{
	FPed& P = Agents[Index];
	if (P.Controller.IsValid())
	{
		P.Controller->UnPossess();
		P.Controller->Destroy();
	}
	if (P.Pawn.IsValid())
	{
		P.Pawn->Destroy();
	}
	Agents.RemoveAtSwap(Index);
}

void AGTAIVPedManager::Tick(float DeltaSeconds)
{
	Super::Tick(DeltaSeconds);
	if (!Network || Adjacent.Num() == 0)
	{
		return;
	}
	APawn* Player = UGameplayStatics::GetPlayerPawn(this, 0);
	PlayerCheckTimer -= DeltaSeconds;
	if (Player && PlayerMesh && PlayerCheckTimer <= 0.f)
	{
		DressPlayer(Player);
		PlayerCheckTimer = 0.5f;
	}
	FVector PlayerLoc = Player ? Player->GetActorLocation() : FVector::ZeroVector;
	if (!Player)
	{
		if (const APlayerCameraManager* Cam = UGameplayStatics::GetPlayerCameraManager(this, 0))
		{
			PlayerLoc = Cam->GetCameraLocation();
		}
	}

	for (int32 i = Agents.Num() - 1; i >= 0; --i)
	{
		FPed& P = Agents[i];
		APawn* Pawn = P.Pawn.Get();
		if (!Pawn || FVector::Dist2D(Pawn->GetActorLocation(), PlayerLoc) > DespawnRadius
			|| Pawn->GetActorLocation().Z < P.GroundZ - 800.f)  // fell through a gap in the collision
		{
			RemoveAgent(i);
			continue;
		}
		const FVector Loc = Pawn->GetActorLocation();
		FVector ToTarget = P.Target - Loc;
		ToTarget.Z = 0.f;
		if (ToTarget.Size() < 120.f)
		{
			PickNext(P);
			ToTarget = P.Target - Loc;
			ToTarget.Z = 0.f;
		}
		Pawn->AddMovementInput(ToTarget.GetSafeNormal(), WalkInput);
		// stuck against something for a while: remove it (a new one spawns elsewhere)
		const float Moved = FVector::Dist2D(Loc, P.LastLocation);
		P.StuckTime = Moved < 20.f * DeltaSeconds ? P.StuckTime + DeltaSeconds : 0.f;
		P.LastLocation = Loc;
		if (P.StuckTime > 6.f)
		{
			RemoveAgent(i);
		}
	}

	const int32 Want = bPedsEnabled ? MaxPeds : 0;
	while (Agents.Num() > Want)
	{
		RemoveAgent(Agents.Num() - 1);
	}
	SpawnTimer -= DeltaSeconds;
	if (Agents.Num() < Want && SpawnTimer <= 0.f)
	{
		// a couple per frame so a full crowd builds up in a second or two
		for (int32 n = 0; n < 2 && Agents.Num() < Want; ++n)
		{
			TrySpawn(PlayerLoc);
		}
		SpawnTimer = 0.05f;
	}
}
