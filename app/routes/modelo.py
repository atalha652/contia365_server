"""
Modelo Routes
API endpoints for managing modelos (tax/legal forms)
"""

from fastapi import APIRouter, HTTPException, Depends, Query
from typing import List, Optional
import logging

from app.models.modelo import (
    ModeloCreate, 
    ModeloResponse, 
    ModeloBulkCreate, 
    ModeloBulkResponse,
    ModeloUpdate
)
from app.repos.modelo_repo import ModeloRepository
from app.routes.auth import get_current_user
from app.routes.spain_tax_dep import require_spanish_tax

# Database connection
from pymongo import MongoClient
import os
import certifi
from dotenv import load_dotenv

load_dotenv()
MONGO_URI = os.getenv("MONGO_URI")
DB_NAME = os.getenv("DB_NAME")

client = MongoClient(MONGO_URI, tlsCAFile=certifi.where())
db = client[DB_NAME]

router = APIRouter(
    tags=["Modelos"],
    dependencies=[Depends(require_spanish_tax)],
)
logger = logging.getLogger(__name__)


def get_modelo_repo() -> ModeloRepository:
    """Get modelo repository instance"""
    return ModeloRepository(db)


@router.post("/modelos", response_model=ModeloResponse, status_code=201)
def create_modelo(
    modelo: ModeloCreate,
    current_user: dict = Depends(get_current_user),
    repo: ModeloRepository = Depends(get_modelo_repo)
):
    """
    Create a new modelo
    
    Creates a single modelo with the provided information.
    """
    try:
        modelo_id = repo.create_modelo(modelo)
        created_modelo = repo.get_modelo(modelo_id)
        
        if not created_modelo:
            raise HTTPException(status_code=500, detail="Failed to retrieve created modelo")
            
        return created_modelo
        
    except ValueError as e:
        raise HTTPException(status_code=400, detail=str(e))
    except Exception as e:
        logger.error(f"Error creating modelo: {e}")
        raise HTTPException(status_code=500, detail="Internal server error")


@router.post("/modelos/bulk", response_model=ModeloBulkResponse, status_code=201)
def create_modelos_bulk(
    bulk_data: ModeloBulkCreate,
    current_user: dict = Depends(get_current_user),
    repo: ModeloRepository = Depends(get_modelo_repo)
):
    """
    Bulk create modelos
    
    Creates multiple modelos at once. Returns summary of successful and failed creations.
    """
    try:
        if not bulk_data.modelos:
            raise HTTPException(status_code=400, detail="No modelos provided")
            
        if len(bulk_data.modelos) > 1000:  # Limit bulk operations
            raise HTTPException(status_code=400, detail="Maximum 1000 modelos per bulk operation")
            
        results = repo.bulk_create_modelos(bulk_data.modelos)
        
        return ModeloBulkResponse(
            success=results["failed_count"] == 0,
            created_count=results["created_count"],
            failed_count=results["failed_count"],
            created_ids=results["created_ids"],
            errors=results["errors"]
        )
        
    except Exception as e:
        logger.error(f"Error in bulk create: {e}")
        raise HTTPException(status_code=500, detail="Internal server error")


@router.get("/modelos/{modelo_id}", response_model=ModeloResponse)
def get_modelo(
    modelo_id: str,
    current_user: dict = Depends(get_current_user),
    repo: ModeloRepository = Depends(get_modelo_repo)
):
    """
    Get modelo by ID
    
    Retrieves a specific modelo by its ID.
    """
    modelo = repo.get_modelo(modelo_id)
    
    if not modelo:
        raise HTTPException(status_code=404, detail="Modelo not found")
        
    return modelo


@router.get("/modelos", response_model=List[ModeloResponse])
def get_modelos(
    skip: int = Query(0, ge=0, description="Number of records to skip"),
    limit: int = Query(100, ge=1, le=1000, description="Maximum number of records to return"),
    current_user: dict = Depends(get_current_user),
    repo: ModeloRepository = Depends(get_modelo_repo)
):
    """
    Get all modelos
    
    Retrieves all modelos with pagination support.
    """
    try:
        modelos = repo.get_all_modelos(skip=skip, limit=limit)
        return modelos
        
    except Exception as e:
        logger.error(f"Error getting modelos: {e}")
        raise HTTPException(status_code=500, detail="Internal server error")


@router.get("/modelos/search/by-number/{modelo_no}", response_model=ModeloResponse)
def get_modelo_by_number(
    modelo_no: str,
    current_user: dict = Depends(get_current_user),
    repo: ModeloRepository = Depends(get_modelo_repo)
):
    """
    Get modelo by modelo number
    
    Retrieves a modelo by its modelo number (e.g., "303", "130").
    """
    modelo = repo.get_modelo_by_number(modelo_no)
    
    if not modelo:
        raise HTTPException(status_code=404, detail=f"Modelo {modelo_no} not found")
        
    return modelo


# Health check endpoint (no auth required)
@router.get("/modelos/health")
def health_check():
    """Health check for modelos service"""
    return {"status": "healthy", "service": "modelos"}


# Test endpoint to create a sample modelo (no auth for testing)
@router.post("/modelos/test")
def create_test_modelo(repo: ModeloRepository = Depends(get_modelo_repo)):
    """Create a test modelo to verify database connection"""
    try:
        test_modelo = ModeloCreate(
            modelo_no="TEST001",
            name="Test Modelo",
            periodicity="Monthly",
            deadline="Test deadline"
        )
        modelo_id = repo.create_modelo(test_modelo)
        return {"message": "Test modelo created", "id": modelo_id}
    except Exception as e:
        return {"error": str(e)}


@router.put("/modelos/{modelo_id}", response_model=ModeloResponse)
def update_modelo(
    modelo_id: str,
    updates: ModeloUpdate,
    current_user: dict = Depends(get_current_user),
    repo: ModeloRepository = Depends(get_modelo_repo)
):
    """
    Update modelo by ID
    
    Updates a modelo with the provided fields. Only non-null fields will be updated.
    """
    try:
        # Check if modelo exists
        existing_modelo = repo.get_modelo(modelo_id)
        if not existing_modelo:
            raise HTTPException(status_code=404, detail="Modelo not found")
        
        # Check if modelo_no is being changed and if it already exists
        if updates.modelo_no and updates.modelo_no != existing_modelo.modelo_no:
            existing_with_same_no = repo.get_modelo_by_number(updates.modelo_no)
            if existing_with_same_no and existing_with_same_no.id != modelo_id:
                raise HTTPException(
                    status_code=400, 
                    detail=f"Modelo with number '{updates.modelo_no}' already exists"
                )
        
        # Perform update
        success = repo.update_modelo(modelo_id, updates)
        if not success:
            raise HTTPException(status_code=500, detail="Failed to update modelo")
        
        # Return updated modelo
        updated_modelo = repo.get_modelo(modelo_id)
        if not updated_modelo:
            raise HTTPException(status_code=500, detail="Failed to retrieve updated modelo")
            
        return updated_modelo
        
    except HTTPException:
        raise
    except Exception as e:
        logger.error(f"Error updating modelo: {e}")
        raise HTTPException(status_code=500, detail="Internal server error")


@router.delete("/modelos/{modelo_id}")
def delete_modelo(
    modelo_id: str,
    current_user: dict = Depends(get_current_user),
    repo: ModeloRepository = Depends(get_modelo_repo)
):
    """
    Delete modelo by ID
    
    Permanently deletes a modelo from the database.
    """
    try:
        # Check if modelo exists
        existing_modelo = repo.get_modelo(modelo_id)
        if not existing_modelo:
            raise HTTPException(status_code=404, detail="Modelo not found")
        
        # Delete modelo
        success = repo.delete_modelo(modelo_id)
        if not success:
            raise HTTPException(status_code=500, detail="Failed to delete modelo")
        
        return {"message": "Modelo deleted successfully", "deleted_id": modelo_id}
        
    except HTTPException:
        raise
    except Exception as e:
        logger.error(f"Error deleting modelo: {e}")
        raise HTTPException(status_code=500, detail="Internal server error")


# Statistics endpoint
@router.get("/modelos/stats")
def get_modelos_stats(
    current_user: dict = Depends(get_current_user),
    repo: ModeloRepository = Depends(get_modelo_repo)
):
    """
    Get modelos statistics
    
    Returns basic statistics about the modelos collection.
    """
    try:
        total_count = repo.count_modelos()
        
        return {
            "total_modelos": total_count,
            "service": "modelos"
        }
        
    except Exception as e:
        logger.error(f"Error getting stats: {e}")
        raise HTTPException(status_code=500, detail="Internal server error")